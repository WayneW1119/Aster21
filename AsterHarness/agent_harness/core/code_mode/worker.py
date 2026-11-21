"""One V8 isolate in a disposable process. Only JSON crosses the host boundary.

No Python callable, filesystem, network or process object is installed in JS.
The parent validates every requested tool against its own catalog and policy.
"""

import json
import os
import select
import sys
import time

BOOTSTRAP = r'''
(() => {
  const queue = [], pending = new Map();
  const data = Object.assign(Object.create(null), INITIAL.store);
  let next = 0, chars = 0, images = 0;
  const copy = value => JSON.parse(JSON.stringify(value));
  const send = message => { queue.push(message); };
  const append = part => {
    if (part.type === 'input_text') {
      chars += part.text.length;
      if (chars > 1000000) throw new Error('Code Mode text capture exceeds 1000000 characters');
    } else if (++images > 4) throw new Error('Code Mode output supports at most 4 images');
    send({type: 'output', part});
  };
  const tools = Object.create(null);
  for (const tool of INITIAL.catalog) {
    tools[tool.name] = (args = {}) => {
      if (args === null || typeof args !== 'object' || Array.isArray(args))
        return Promise.reject(new Error('Tool arguments must be a JSON object'));
      const id = ++next;
      return new Promise((resolve, reject) => {
        pending.set(id, {resolve, reject});
        send({type:'call', id, name:tool.name, arguments:copy(args)});
      });
    };
  }
  const bridge = Object.freeze({
    drain: () => { const batch = queue.splice(0); return batch; },
    resolve: message => {
      const waiter = pending.get(message.id);
      if (!waiter) return;
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(message.error));
      else waiter.resolve(message.result);
    },
    finish: () => send({type:'completed'}),
    fail: error => send({type:'failed', error:String(error && error.stack || error)}),
  });
  Object.defineProperties(globalThis, {
    tools: {value:Object.freeze(tools)},
    ALL_TOOLS: {value:Object.freeze(INITIAL.catalog.map(v => Object.freeze(v)))},
    text: {value:value => append({type:'input_text', text:typeof value === 'string' ? value :
      value === undefined ? 'undefined' : JSON.stringify(value)})},
    image: {value:value => {
      const parts = value && (value.output || value.content) || value;
      const images = Array.isArray(parts) ? parts.filter(p => p.type === 'input_image') :
        [{type:'input_image', image_url:typeof parts === 'string' ? parts : parts && parts.image_url}];
      if (!images.length) throw new Error('image() requires native image data; use tools.view_image first');
      for (const part of images) {
        if (typeof part.image_url !== 'string' || !/^data:image\/(png|jpeg|webp|gif);base64,/.test(part.image_url))
          throw new Error('image() requires an image data URL, not a path or network URL');
        append(copy(part));
      }
    }},
    store: {value:(key,value) => {
      if (typeof key !== 'string' || !key.length || key.length > 128) throw new Error('Invalid store key');
      const saved = copy(value);
      const candidate = Object.assign(Object.create(null), data, {[key]:saved});
      if (JSON.stringify(candidate).length > 2000000) throw new Error('Temporary store exceeds its limit');
      data[key] = saved;
      send({type:'store', key, value:saved});
    }},
    load: {value:key => Object.hasOwn(data,key) ? copy(data[key]) : undefined},
    __bridge: {value:bridge},
  });
  delete globalThis.INITIAL;
  delete globalThis.console;
})();
'''


def emit(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


def main():
    from py_mini_racer import MiniRacer

    incoming = b''
    while b'\n' not in incoming:
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            raise EOFError('Missing cell input')
        incoming += chunk
    line, incoming = incoming.split(b'\n', 1)
    initial = json.loads(line)
    windows_input = None
    if os.name == 'nt':
        import queue, threading
        windows_input = queue.Queue()
        def read_input():
            while True:
                chunk = os.read(sys.stdin.fileno(), 65536)
                windows_input.put(chunk)
                if not chunk: return
        threading.Thread(target=read_input, daemon=True).start()
    with MiniRacer() as isolate:
        isolate.set_hard_memory_limit(128 * 1024 * 1024)
        isolate.eval('globalThis.INITIAL = ' + json.dumps(initial, ensure_ascii=True), timeout_sec=.5)
        isolate.eval(BOOTSTRAP, timeout_sec=.5)
        code = '(async () => {\n' + initial['code'] + '\n})().then(__bridge.finish, __bridge.fail);'
        isolate.eval(code, timeout_sec=.5)
        while True:
            batch = isolate.execute('__bridge.drain()', timeout_sec=.5)
            terminal = False
            for message in batch:
                emit(message)
                terminal |= message['type'] in {'completed', 'failed'}
            if terminal:
                return
            if windows_input is not None:
                try: chunk = windows_input.get(timeout=.005)
                except queue.Empty: chunk = None
                if chunk == b'': return
                if chunk: incoming += chunk
            elif select.select([sys.stdin], [], [], .005)[0]:
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    return
                incoming += chunk
            while b'\n' in incoming:
                line, incoming = incoming.split(b'\n', 1)
                isolate.eval('__bridge.resolve(' + json.dumps(json.loads(line), ensure_ascii=True) + ')', timeout_sec=.5)


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        emit({'type':'failed', 'error':f'{type(error).__name__}: {error}'[:2000]})
