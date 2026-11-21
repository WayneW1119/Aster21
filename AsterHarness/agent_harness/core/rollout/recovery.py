"""Evidence for explicit crash recovery; never repair partial tool executions."""
from copy import deepcopy
import hashlib
import json

from .runtime_checkpoint import validate_checkpoint


def digest(rows):
    value = hashlib.sha256()
    for row in rows:
        value.update(json.dumps(row, sort_keys=True, separators=(',', ':')).encode())
        value.update(b'\n')
    return value.hexdigest()


def stop_uncertain_once(checkpoint):
    from ..event_generators.manager import EventGeneratorManager

    validate_checkpoint(checkpoint)
    sources = checkpoint['generators']
    finished = [s for s in sources if s['record']['status'] != 'active']
    interrupted = 0
    for snapshot in sources:
        record = snapshot['record']
        if record['kind'] == 'program' and record['status'] == 'active' and record['configuration']['mode'] == 'once':
            record.update(status='failed', error='Interrupted by an abrupt stop; outcome unknown. No automatic replay.')
            interrupted += 1
    overflow = len(finished) + interrupted - EventGeneratorManager.MAX_FINISHED
    if overflow > 0:
        expired = {s['record']['generator_id'] for s in finished[:overflow]}
        checkpoint['generators'] = [s for s in sources if s['record']['generator_id'] not in expired]
    return checkpoint


def advance_source(generators, event):
    if event['kind'] not in {'program_event', 'timer_fired', 'command_completed', 'generator_failed', 'generator_rate_limited', 'generator_unavailable', 'code_execution_completed'}:
        return
    item = event['item']
    text = item.get('text', '')
    if not isinstance(text, str) or not text.startswith('<harness_event>'):
        return
    body = json.loads(text.removeprefix('<harness_event>').removesuffix('</harness_event>'))
    if body.get('source') != 'event_generator':
        return
    snapshot = generators.get(body.get('generator_id'))
    if snapshot is None:
        raise ValueError('Recovery has an event from an unknown generator')
    record = snapshot['record']; sequence = body.get('sequence')
    if type(sequence) is not int or sequence != record['events_emitted'] + 1:
        raise ValueError('Recovery generator sequence is incomplete')
    if record['kind'] == 'timer':
        raise ValueError('Recovery cannot infer a timer deadline after an uncheckpointed firing')
    record['events_emitted'] = sequence
    if body.get('kind') in {'generator_failed', 'generator_rate_limited'}:
        record.update(status='failed', error='Source failed before the abrupt stop; inspect before restarting.')


def from_boundary(rows, start):
    checkpoints = [i for i in range(start, len(rows)) if rows[i]['type'] == 'runtime_checkpoint'
                   and rows[i]['payload'].get('boundary') in {'model_request', 'step_complete'}]
    if not checkpoints:
        return None
    index = checkpoints[-1]
    checkpoint = deepcopy(rows[index]['payload'])
    validate_checkpoint(checkpoint)
    queue = {e['event_id']: e for e in checkpoint['pending_events']}
    generators = {s['record']['generator_id']: s for s in checkpoint['generators']}
    for row in rows[index+1:]:
        # A tool batch or partially archived next Step is never guessed/replayed.
        if row['type'] != 'event_queued':
            raise ValueError('Crash occurred outside a recoverable runtime boundary')
        event = row['payload']
        if event['event_id'] in queue:
            raise ValueError('Duplicate recovery event ID')
        queue[event['event_id']] = deepcopy(event)
        advance_source(generators, event)
    checkpoint['pending_events'] = list(queue.values())
    return stop_uncertain_once(checkpoint)


def from_trace(rows, start, path):
    """Opt-in bridge for older Threads without durable Step checkpoints.

    The trace must prove a complete queue history and an uncommitted model
    request. The caller additionally compares its effective history to the
    fully validated authoritative rollout before appending any recovery marker.
    """
    sid = rows[start]['payload']['session_id']
    raw = (path / 'traces' / ('sdk-' + sid + '.jsonl')).read_bytes()
    if not raw.endswith(b'\n'):
        raise ValueError('Recovery trace is incomplete')
    traces = [json.loads(line) for line in raw.splitlines()]
    if any(not isinstance(t, dict) or not isinstance(t.get('event'), str)
           or ('session_id' in t and t['session_id'] != sid) for t in traces):
        raise ValueError('Recovery trace belongs to another Session or is invalid')
    previous = None
    for row in rows[:start]:
        if row['type'] == 'runtime_checkpoint':
            previous = row['payload']
        elif row['type'] == 'session_recovered' and row['payload'].get('version') == 2:
            previous = row['payload']['checkpoint']
    initial = deepcopy(previous or {'pending_events': [], 'generators': [], 'step_number': 0})
    queue = {e['event_id']: e for e in initial['pending_events']}
    generators = {s['record']['generator_id']: s for s in initial['generators']}
    enqueued = []; last_phase = None; request = None; history = None; initialized = False
    for trace in traces:
        kind = trace['event']
        if kind == 'session_initialized':
            if trace.get('step') != initial['step_number']:
                raise ValueError('Recovery trace does not continue the previous checkpoint')
            initialized = True
        if kind == 'event_enqueued':
            event = trace['queued_event']
            if event['event_id'] in queue:
                raise ValueError('Duplicate recovery trace event')
            queue[event['event_id']] = event
            enqueued.append(event)
        elif kind == 'step_started':
            for event in trace['events']:
                if queue.pop(event['event_id'], None) != event:
                    raise ValueError('Recovery trace is missing an input event')
        elif kind == 'history_updated':
            history = trace['items']
        if kind in {'step_started', 'model_request', 'model_response', 'step_completed', 'tool_started'}:
            last_phase = kind
        if kind == 'model_request':
            request = trace
        if 'generator' in trace and kind.startswith('generator_'):
            record = deepcopy(trace['generator']); ident = record['generator_id']
            state = generators.get(ident, {}).get('state')
            generators[ident] = {'record': record, 'state': state}
        elif kind == 'generator_fired':
            event = trace['generated_event']; ident = event['generator_id']
            if ident not in generators or event['sequence'] != generators[ident]['record']['events_emitted'] + 1:
                raise ValueError('Recovery trace generator sequence is incomplete')
            generators[ident]['record']['events_emitted'] = event['sequence']
            if generators[ident]['record']['kind'] == 'timer':
                raise ValueError('Legacy trace cannot recover changed timer deadlines')
    journal = [row['payload'] for row in rows[start:] if row['type'] == 'event_queued']
    if journal != enqueued:
        raise ValueError('Recovery trace and durable event journal disagree')
    if not initialized or last_phase != 'model_request' or history is None or request is None:
        raise ValueError('Legacy trace does not prove an uncommitted model request')
    checkpoint = {'version': 1, 'session_id': sid, 'step_number': request['step'],
                  'pending_events': list(queue.values()), 'generators': list(generators.values())}
    return stop_uncertain_once(checkpoint), history, hashlib.sha256(raw).hexdigest()


def crash_evidence(rows, path, allow_trace):
    start = max(i for i, row in enumerate(rows) if row['type'] == 'session_opened')
    checkpoint = from_boundary(rows, start)
    # A resume can fail before its first checkpoint. Only an entirely empty
    # resumed Session may reuse the preceding, fully validated clean close.
    if (checkpoint is None and start == len(rows) - 1 and start >= 2
            and rows[start]['payload'].get('resumed') is True
            and rows[start-1]['type'] == 'session_closed'
            and rows[start-1]['payload'].get('clean') is True
            and rows[start-2]['type'] == 'runtime_checkpoint'):
        from .recorder import RolloutRecorder
        RolloutRecorder._read(''.join(json.dumps(row) + '\n' for row in rows[:start]),
                             rows[0]['payload']['thread_id'])
        checkpoint = deepcopy(rows[start-2]['payload'])
        checkpoint['session_id'] = rows[start]['payload']['session_id']
        checkpoint = stop_uncertain_once(checkpoint)
    if checkpoint is not None:
        return checkpoint, None, 'boundary', None
    if not allow_trace:
        raise ValueError('No recoverable runtime boundary checkpoint; legacy SDK trace recovery requires allow_trace=True')
    checkpoint, history, trace_hash = from_trace(rows, start, path)
    return checkpoint, history, 'trace', trace_hash
