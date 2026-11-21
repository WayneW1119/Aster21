"""JSON-function Code Mode entrypoints, grouped without renaming existing tools."""

from .registry import Tool


def register_code_mode_tools(registry, runtime):
    common = ('JavaScript composition uses existing tools through tools.<name>({...}). '
        'Nested results are {output: string or native content parts}; use text(value) or image(result) '
        'to choose model-visible output. Every direct exec/wait receipt enters the normal event queue. '
        'Nested results return to JavaScript and are fully traced, without synthetic model calls. '
        'Sending text() does not message the user. Use tools.send_user_message for the basic built-in channel, '
        'or the corresponding tools for other connected channels. '
        'ALL_TOOLS lists callable names/descriptions; default functions members use bare names; other members use namespace__member names. '
        'Deferred tools must first be loaded by a direct tool_search; compact removes covered definitions. '
        'Memory writes, compact, tool registration, nested search and recursive exec/wait are unavailable; '
        'call their normal tools directly. Do not use commands to bypass private-memory tools. '
        'No Node/Python/filesystem/network globals are provided. Always await tool calls, including Promise.all. '
        'store(key,value)/load(key) exchange JSON data across cells in this live Session; a cell reads its '
        'initial store snapshot plus its own writes. Stored data is not memory or WorldState and is lost on restart. ')
    try:
        description = registry.namespace_description('functions')
    except KeyError:
        description = 'JavaScript tool composition and execution progress.'
    registry.register_namespace('functions', description, [
        Tool({'type':'function','name':'exec','strict':False,'description':common +
            'Run JavaScript with top-level await. A completed script returns immediately; a longer script '
            'returns running and cell_id, then its registered source emits code_execution_completed. '
            'Use wait to collect output; do not poll merely to keep running. Each cell has a fresh V8 isolate. '
            'Returned completed means script execution finished, not that every business task was verified.',
            'parameters':{'type':'object','properties':{
                'code':{'type':'string','description':'JavaScript source, 1-100000 characters.'},
                'yield_time_ms':{'type':'integer','description':'Wait before returning, 0-30000 ms; default 1000.'},
                'timeout_ms':{'type':'integer','description':'Cell wall-time limit, 100-300000 ms; default 30000.'}},
                'required':['code'],'additionalProperties':False}}, runtime.execute),
        Tool({'type':'function','name':'wait','strict':False,'description':
            'Read new output and status from a cell returned by functions.exec. Returns running, completed, '
            'failed or cancelled, with cell_id and only unread output. A completion event wakes the Agent '
            'when needed; wait_for_event can wait quietly. terminate stops JS but cannot undo already '
            'dispatched tool effects or automatically stop command processes. Cells never resume after restart.',
            'parameters':{'type':'object','properties':{
                'cell_id':{'type':'string','description':'Live cell_id returned by functions.exec.'},
                'yield_time_ms':{'type':'integer','description':'Wait for progress, 0-30000 ms; default 1000.'},
                'terminate':{'type':'boolean','description':'Stop this script if running; default false.'}},
                'required':['cell_id'],'additionalProperties':False}}, runtime.wait)])
