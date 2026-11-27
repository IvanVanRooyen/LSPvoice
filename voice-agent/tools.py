"""Tool definitions shared by the OpenAI and Claude reply loops, plus input validation.

Specs are written once here and converted to each provider's format. Every tool input is
validated against its schema in code before it runs, whichever provider produced it.
"""

TICKET_FIELDS = {
    'hospital': 'Hospital name or code as the caller gave it.',
    'bed_id': 'Bed number, or an empty string.',
    'device_id': 'Device ID, or an empty string.',
    'caller_name': "Caller's name as confirmed in the read-back.",
    'callback_number': 'Australian phone number for a callback, as confirmed in the read-back.',
    'issue_summary': "One or two sentences describing the problem in the caller's words.",
    'steps_tried': 'Approved steps already tried on this call, or an empty string.',
}


def _string(description=None, enum=None):
    schema = {'type': 'string'}
    if description:
        schema['description'] = description
    if enum:
        schema['enum'] = list(enum)
    return schema


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def flow_specs(flows):
    """Tools for the flow engine (flows.py). The list is the same whatever step a call is at."""
    if not flows:
        return []
    menu = ' '.join(f"{fid}: {flow['use_when']}" for fid, flow in sorted(flows.items()))
    fields = sorted({name for flow in flows.values() for name in flow['fields']})
    return [
        {'name': 'start_flow',
         'description': 'Start a guided process. Available: ' + menu,
         'parameters': _object({'flow_id': _string(enum=sorted(flows))})},
        {'name': 'flow_record',
         'description': ('Record details for the active flow as soon as the caller gives them (several at once is fine). '
                         'Each value is checked; the result says what is still needed and what to say next.'),
         'parameters': _object({'values': {'type': 'array', 'items': _object({
             'field': _string(enum=fields), 'value': _string("The caller's answer (true/false for yes-no fields).")})}})},
        {'name': 'flow_confirm',
         'description': ("After reading back the details and hearing the caller's answer: yes if they said the details are "
                         'correct, no if something needs changing. A yes runs the flow\'s action (for example logging the ticket).'),
         'parameters': _object({'answer': _string(enum=['yes', 'no'])})},
        {'name': 'flow_cancel',
         'description': 'Stop the active flow because the caller no longer wants it.',
         'parameters': _object({'reason': _string()})},
    ]


def specs(guide_ids, search=False, tickets=True, feedback=False, flows=None):
    """flows: flow definitions; when given, tickets are only logged through a flow (no create_ticket tool)."""
    if flows:
        tickets = False
    found = [
        {'name': 'lookup_device',
         'description': ('Read-only, hospital-scoped inventory lookup. Pass an empty string for bed_id '
                         'or device_id when the caller has not given it.'),
         'parameters': _object({'hospital': _string(), 'bed_id': _string(), 'device_id': _string()})},
        {'name': 'finish_call',
         'description': 'End the call: reason clinical for a clinical redirect, goodbye when the caller is done.',
         'parameters': _object({'reason': _string(enum=['clinical', 'goodbye'])})},
    ]
    if guide_ids:
        found.append({
            'name': 'get_guide',
            'description': ('Fetch one reviewed guide by its exact ID from the GUIDES AVAILABLE list. '
                            'Fetch it before giving any step it covers.'),
            'parameters': _object({'guide_id': _string(enum=guide_ids)})})
    if search:
        found.append({
            'name': 'search_knowledge',
            'description': ('Search reviewed LSPcare reference material for specifics not covered above. '
                            'Returns up to three sections with their source IDs.'),
            'parameters': _object({'query': _string('A few keywords describing what is needed.')})})
    if tickets:
        properties = {name: _string(text) for name, text in TICKET_FIELDS.items()}
        properties['urgent'] = {'type': 'boolean',
                                'description': 'True when the caller paid and service is not working, or was charged twice.'}
        properties['caller_confirmed'] = {'type': 'boolean',
                                          'description': 'True only after you read the details back and the caller said yes.'}
        found.append({
            'name': 'create_ticket',
            'description': ('Log one LSPcare support ticket for follow-up. Only after reading the details back '
                            'and getting a clear yes. At most one per call.'),
            'parameters': _object(properties)})
    if feedback:
        found.append({
            'name': 'report_problem',
            'description': ("Staff pilot only: record a tester's report of a problem with how you handled this call "
                            "(wrong answer, misheard, confusing, too slow). Not for equipment faults: use create_ticket "
                            "for those."),
            'parameters': _object({'description': _string("What went wrong, in the caller's own words.")})})
    return found + flow_specs(flows)


def openai_tools(found):
    return [{'type': 'function', 'name': t['name'], 'description': t['description'],
             'parameters': t['parameters'], 'strict': True} for t in found]


def anthropic_tools(found):
    return [{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters'],
             'eager_input_streaming': True} for t in found]


def valid_input(found, name, args):
    """Exact-schema check: required keys only, right types, enum values respected."""
    spec = next((t for t in found if t['name'] == name), None)
    return spec is not None and _matches(spec['parameters'], args)


def _matches(schema, value):
    kind = schema['type']
    if kind == 'object':
        properties = schema['properties']
        return (isinstance(value, dict) and set(value) == set(properties)
                and all(_matches(properties[k], value[k]) for k in properties))
    if kind == 'array':
        return isinstance(value, list) and 0 < len(value) <= 20 and all(_matches(schema['items'], v) for v in value)
    if kind == 'string':
        return isinstance(value, str) and ('enum' not in schema or value in schema['enum'])
    if kind == 'boolean':
        return isinstance(value, bool)
    return False
