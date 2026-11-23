"""mcp_server.py: the local MCP. The tool list is QA Link Cloud's (src/data/mcp-tools.ts) as its JSON export,
pc/bridge/mcp-tools.json (`node tools/gen-mcp-doc.mjs --json`); nothing here lists tools a second time. Each tool is the
same bridge call as in the app's mock MCP, made over contract 1 to a running bridge, so stdio (Claude Code or Codex start
it themselves) and HTTP (started by the bridge) behave the same. Every call is logged with who made it and its level,
here (mcp-calls.log beside the bridge's data) and in the bridge's calls.log (X-By: mcp <client>).

Needs the Python MCP SDK (`pip install mcp`, 2.x)."""
import base64, hashlib, json, os, re, time, urllib.error, urllib.parse, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_JSON = os.path.join(HERE, 'mcp-tools.json')
LEVEL_RANK = {'read': 0, 'control': 1, 'run': 2}


def load_tools():
    with open(TOOLS_JSON, encoding='utf-8') as f:
        d = json.load(f)
    return d


def check_args(schema, v, at='arguments'):
    """The same subset of JSON Schema as mcp-tools.ts checkArgs."""
    out = []
    t = schema.get('type')
    if t is None:
        return out
    if t == 'integer':
        if not isinstance(v, int) or isinstance(v, bool):
            return ['%s must be a whole number' % at]
    elif t == 'number':
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return ['%s must be a number' % at]
    else:
        pyt = {'object': dict, 'string': str, 'boolean': bool, 'array': list}.get(t)
        if pyt and not isinstance(v, pyt):
            return ['%s must be %s' % (at, 'an object' if t == 'object' else 'a ' + t)]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if schema.get('minimum') is not None and v < schema['minimum']:
            out.append('%s must be at least %s' % (at, schema['minimum']))
        if schema.get('maximum') is not None and v > schema['maximum']:
            out.append('%s must be at most %s' % (at, schema['maximum']))
    if isinstance(v, str):
        if schema.get('maxLength') is not None and len(v) > schema['maxLength']:
            out.append('%s is too long' % at)
        if schema.get('pattern') and not re.search(schema['pattern'], v):
            out.append('%s is not in the expected form' % at)
    if 'enum' in schema and v not in schema['enum']:
        out.append('%s must be one of %s' % (at, ', '.join(map(str, schema['enum']))))
    if t == 'array' and isinstance(v, list) and schema.get('items'):
        for i, x in enumerate(v):
            out += check_args(schema['items'], x, '%s[%d]' % (at, i))
    if t == 'object' and isinstance(v, dict):
        for r in schema.get('required') or []:
            if r not in v:
                out.append('%s.%s is required' % (at, r))
        for k, x in v.items():
            p = (schema.get('properties') or {}).get(k)
            if p is None:
                if schema.get('additionalProperties') is False:
                    out.append('%s.%s is not a known input' % (at, k))
                continue
            out += check_args(p, x, '%s.%s' % (at, k))
    return out


class BridgeClient:
    def __init__(self, base, who):
        self.base = base.rstrip('/')
        self.who = who

    def call(self, method, path, body=None, ctype='application/json', raw=False, timeout=60):
        data = json.dumps(body).encode() if (body is not None and ctype == 'application/json') else body
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={'Content-Type': ctype, 'X-By': self.who, 'X-Contract': '1'})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as f:
                b = f.read()
                return f.status, (b if raw else json.loads(b or b'{}'))
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b or b'{}')
            except ValueError:
                return e.code, {'error': b.decode('latin-1')[:300]}
        except (urllib.error.URLError, OSError) as e:
            return 503, {'error': 'no bridge at %s (%s): start it with `python pc/qalink_bridge.py`' % (self.base, e)}

    def push(self, args):
        if args.get('path'):
            raw = open(args['path'], 'rb').read()
            name = args.get('name') or os.path.basename(args['path'])
        elif args.get('base64'):
            raw = base64.b64decode(args['base64'])
            name = args.get('name') or 'build.elf'
        else:
            return 400, {'error': 'give path (a file on the bridge\'s PC) or base64'}
        bnd = uuid.uuid4().hex
        fields = [('sha256', hashlib.sha256(raw).hexdigest()), ('name', name), ('args', args.get('args', '')),
                  ('deadline_s', str(args.get('deadline_s', 0)))]
        body = b''.join(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (bnd, k, v)).encode()
                        for k, v in fields)
        body += ('--%s\r\nContent-Disposition: form-data; name="program"; filename="%s"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n' % (bnd, name)).encode() + raw + b'\r\n'
        body += ('--%s--\r\n' % bnd).encode()
        return self.call('POST', '/v0/runs', body, 'multipart/form-data; boundary=%s' % bnd)

    def wait_run(self, rid, timeout_s):
        end = time.time() + timeout_s
        while True:
            code, r = self.call('GET', '/v0/runs/%s' % rid)
            if code != 200 or r.get('state') not in ('queued', 'sending', 'running') or time.time() > end:
                return code, r
            time.sleep(0.5)


def prune(v):
    if isinstance(v, dict):
        return {k: prune(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [prune(x) for x in v if x is not None]
    return v


def q(d):
    return '?' + urllib.parse.urlencode({k: v for k, v in d.items() if v is not None}) if d else ''


def handle(bc, name, a):
    """(structured result or None, error text or None, image PNG bytes or None) for one tool call."""
    cmd = lambda body: bc.call('POST', '/v0/command', body)
    if name == 'link_status':
        c, r = bc.call('GET', '/v0/status')
    elif name == 'list_devices':
        c, r = bc.call('GET', '/v0/devices')
        if c == 200:
            c2, d = bc.call('GET', '/v0/discovery')
            r['discovery'] = d.get('seen', []) if c2 == 200 else []
    elif name == 'push_build':
        c, r = bc.push(a)
    elif name == 'run_and_wait':
        c, r = bc.push(a)
        if c == 202:
            c, r = bc.wait_run(r['id'], a.get('timeout_s', 60))
    elif name == 'get_run':
        if a.get('id'):
            c, r = bc.call('GET', '/v0/runs/%s' % a['id'])
            r = {'runs': [r]} if c == 200 else r
        else:
            c, r = bc.call('GET', '/v0/runs')
    elif name == 'self_test':
        c, r = bc.call('POST', '/v0/selftest', {})
        if c == 202:
            c, r = bc.wait_run(r['id'], a.get('timeout_s', 30))
    elif name == 'grab_screen':
        c, r = bc.call('POST', '/v0/stills', {})
        if c == 200:
            c2, png = bc.call('GET', '/v0/stills/%s' % r['id'], raw=True)
            if c2 == 200:
                return ({'n': r['picture'], 'w': r['width'], 'h': r['height'], 'label': 'still %s' % r['id'],
                         'bytes': len(png), 'id': r['id'], 'tier': r.get('tier')}, None, png)
            c, r = c2, {'error': 'the still arrived but its picture could not be read'}
    elif name == 'live_summary':
        c, r = bc.call('GET', '/v0/summary')
    elif name == 'slowest_pictures':
        c, r = bc.call('GET', '/v0/summary' + q({'slowest': a.get('count', 10)}))
        if c == 200:
            r = {'tier': r['tier'], 'slowest': r['slowest']}
    elif name == 'query_trace':
        c, r = bc.call('GET', '/v0/trace' + q({k: a.get(k) for k in ('from', 'to', 'lane', 'limit')}))
    elif name == 'read_log':
        c, r = bc.call('GET', '/v0/log' + q({k: a.get(k) for k in ('from', 'to', 'level', 'q', 'limit')}))
    elif name == 'list_presets':
        c, r = bc.call('GET', '/v0/presets')
    elif name == 'warp':
        c, r = cmd({'cmd': 'warp', 'preset': a['preset']})
    elif name in ('pause', 'resume'):
        c, r = cmd({'cmd': name})
    elif name == 'step':
        c, r = cmd({'cmd': 'step', 'count': a.get('count', 1)})
    elif name == 'set_option':
        c, r = cmd({'cmd': 'option', 'name': a['name'], 'value': a['value']})
    elif name == 'send_pad':
        c, r = cmd(dict({'cmd': 'pad'}, **{k: a[k] for k in ('state', 'steps') if k in a}))
    elif name == 'reset':
        c, r = cmd({'cmd': 'reset', 'level': a['level']})
    elif name == 'read_memory':
        c, r = bc.call('POST', '/v0/memory/read', {'addr': int(a['addr'], 16), 'len': a['len']})
    elif name == 'upload_asset':
        c, r = bc.call('POST', '/v0/assets', {k: a[k] for k in ('path', 'base64', 'name') if k in a})
    elif name == 'list_sessions':
        c, r = bc.call('GET', '/v0/sessions')
    elif name == 'export_session':
        c, raw = bc.call('GET', '/v0/sessions/%s' % urllib.parse.quote(a['id']), raw=True)
        if c == 200:
            mx = a.get('max_bytes', 1000000)
            return ({'id': a['id'], 'bytes': len(raw), 'truncated': len(raw) > mx,
                     'text': raw[:mx].decode('utf-8', 'replace')}, None, None)
        r = json.loads(raw or b'{}') if isinstance(raw, bytes) else raw
    else:
        return None, 'unknown tool %s' % name, None
    if c >= 400:
        return None, '%s: bridge answered %d: %s' % (name, c, r.get('error') if isinstance(r, dict) else r), None
    return r, None, None


class Mcp:
    def __init__(self, bridge_url, level, log_path=None):
        self.d = load_tools()
        self.level = level
        self.bridge_url = bridge_url
        self.tools = [t for t in self.d['tools'] if LEVEL_RANK[t['level']] <= LEVEL_RANK[level]]
        self.by_name = {t['name']: t for t in self.tools}
        self.log_path = log_path

    def log(self, who, tool, level, ok, detail):
        if not self.log_path:
            return
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'who': who, 'tool': tool,
                                'level': level, 'ok': ok, 'detail': str(detail)[:300]}) + '\n')

    def server(self):
        import mcp_types as types
        from mcp.server.lowlevel import Server

        async def list_tools(ctx, params):
            return types.ListToolsResult(tools=[types.Tool(name=t['name'], title=t['title'], description=t['description'],
                                                           input_schema=t['inputSchema'], output_schema=t['outputSchema'])
                                                for t in self.tools])

        async def call_tool(ctx, params):
            import anyio
            name, a = params.name, params.arguments or {}
            who = 'mcp'
            try:
                ci = ctx.session.client_params.client_info
                who = 'mcp %s %s' % (ci.name, ci.version)
            except AttributeError:
                pass
            t = self.by_name.get(name)
            if t is None:
                msg = 'no tool %s at level %s' % (name, self.level)
                self.log(who, name, None, False, msg)
                return types.CallToolResult(content=[types.TextContent(text=msg)], is_error=True)
            probs = check_args(t['inputSchema'], a)
            if probs:
                self.log(who, name, t['level'], False, probs)
                return types.CallToolResult(content=[types.TextContent(text='; '.join(probs))], is_error=True)
            bc = BridgeClient(self.bridge_url, who)
            r, err, png = await anyio.to_thread.run_sync(handle, bc, name, a)
            self.log(who, name, t['level'], err is None, err or (r.get('id') if isinstance(r, dict) else ''))
            if err:
                return types.CallToolResult(content=[types.TextContent(text=err)], is_error=True)
            r = prune(r)   # the list's schemas type each field; an unknown value is left out, never sent as null
            content = [types.TextContent(text=json.dumps(r)[:20000])]
            if png is not None:
                content.insert(0, types.ImageContent(data=base64.b64encode(png).decode(), mime_type='image/png'))
            return types.CallToolResult(content=content, structured_content=r)

        return Server('dc-qalink', version=self.d.get('version', '0.2.0'), title='dc-qalink bridge (local MCP)',
                      instructions='A Dreamcast or Flycast linked through dc-qalink\'s bridge. Call link_status first. '
                                   'Every number carries its tier (console, flycast or mock).',
                      on_list_tools=list_tools, on_call_tool=call_tool)


def run_stdio(bridge_url, level, log_dir=None):
    import anyio
    from mcp.server.stdio import stdio_server
    log_dir = log_dir or os.path.join(os.path.expanduser('~'), '.dc-qalink', 'bridge')
    os.makedirs(log_dir, exist_ok=True)
    m = Mcp(bridge_url, level, os.path.join(log_dir, 'mcp-calls.log'))
    srv = m.server()

    async def main():
        async with stdio_server() as (r, w):
            await srv.run(r, w, srv.create_initialization_options())
    anyio.run(main)
    return 0


def start_http(bridge_url, port, level, log, log_dir=None):
    """The MCP over streamable HTTP on 127.0.0.1:<port>/mcp, in a thread of the bridge."""
    import threading
    import uvicorn
    log_dir = log_dir or os.path.join(os.path.expanduser('~'), '.dc-qalink', 'bridge')
    os.makedirs(log_dir, exist_ok=True)
    m = Mcp(bridge_url, level, os.path.join(log_dir, 'mcp-calls.log'))
    app = m.server().streamable_http_app(host='127.0.0.1')
    cfg = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
    th = threading.Thread(target=uvicorn.Server(cfg).run, daemon=True, name='bridge-mcp-http')
    th.start()
    log('MCP (streamable HTTP) on http://127.0.0.1:%d/mcp, level %s, %d tools' % (port, level, len(m.tools)))
    return th
