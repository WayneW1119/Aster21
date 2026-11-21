"""Small, content-free projections of public Harness trace events."""
import time


PHASES = {
    'session_initialized': 'starting', 'step_started': 'processing',
    'model_request': 'thinking', 'model_retrying': 'retrying',
    'model_response': 'processing', 'tool_result': 'processing',
    'step_completed': 'processing', 'session_waiting': 'waiting',
    'session_stopped': 'stopped', 'session_error': 'error',
}


def count(value):
    return value if type(value) is int and value >= 0 else None


def usage_counts(usage):
    usage = usage if isinstance(usage, dict) else {}
    inputs = count(usage.get('input_tokens', usage.get('prompt_tokens')))
    outputs = count(usage.get('output_tokens', usage.get('completion_tokens')))
    details = usage.get('input_tokens_details', usage.get('prompt_tokens_details'))
    cached = count(details.get('cached_tokens')) if isinstance(details, dict) else None
    if cached is None:
        cached = count(usage.get('prompt_cache_hit_tokens'))
    if inputs is None or cached is not None and cached > inputs:
        cached = None
    return {'input_tokens': inputs, 'output_tokens': outputs, 'cached_tokens': cached}


def trace_update(event, data):
    """Never forward prompts, arguments, results, errors or upstream bodies."""
    phase = PHASES.get(event)
    update = {}
    if event == 'tool_started':
        call = data.get('call', {})
        name = call.get('name', '')
        update['tool'] = name[:128] if isinstance(name, str) else ''
        phase = 'waiting' if name == 'wait_for_event' else 'tools'
    if phase:
        update['phase'] = phase
    if event == 'model_response':
        update['usage'] = usage_counts(data.get('usage'))
    if event == 'history_compacted':
        update['context_compacted'] = True
    if not update:
        return None
    update.update(step=count(data.get('step')), observed_at=time.time())
    return update


def merge(previous, update, model):
    """Keep a bounded window across worker restarts; missing usage stays unknown."""
    result = dict(previous or {})
    observed = update['observed_at']
    phase = update.get('phase')
    if phase:
        result.update(phase=phase, tool=update.get('tool'))
    result['updated_at'] = observed
    if update.get('step') is not None:
        result['step'] = update['step']
    if 'usage' in update:
        sample = {**update['usage'], 'at': observed, 'step': update.get('step'), 'model': model}
        # A different model has an independent context and cache population.
        recent = result.get('recent', []) if result.get('model') == model else []
        result.update(model=model, latest=sample, recent=[*recent, sample][-20:], context_compacted=False)
        known = [s for s in result['recent'] if s['input_tokens'] is not None and s['cached_tokens'] is not None]
        inputs = sum(s['input_tokens'] for s in known)
        cached = sum(s['cached_tokens'] for s in known)
        result['cache_window'] = {'requests': len(result['recent']), 'reported': len(known),
                                  'input_tokens': inputs, 'cached_tokens': cached,
                                  'hit_rate': cached / inputs if inputs else None}
    if update.get('context_compacted'):
        result['context_compacted'] = True
    return result
