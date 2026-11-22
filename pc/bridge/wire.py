"""wire.py: the live-data additions to the control loop's protocol 2 (include/dcql_loop.h keeps the C side in step).

Additive: a loader or client that does not know a type ignores it, so 0.2's link core and 0.1.0's modem API are unchanged.

console -> PC
  REC     0x0d  a = picture of the first record, b = records; payload = records, each
                  u32 n, u16 flags, u8 nph, u8 nctr, u32 phase_us[nph], i32 ctr[nctr], then if flags & 1: u32 room, f32 pos[3]
  LIVEDEF 0x0e  a = declaration version, b = part | parts << 8; payload = text lines (see parse_def)
  TRACE   0x0f  a = sequence number of the first event, b = events; payload = events, each
                  u32 n, u16 d (pictures), u8 lane, u8 len, label[len]   (a jump in the sequence is lost events: a gap)
  LINE    0x07  b = 0: a serial line (as before); b = 1..4: a game log line (info, warn, err, stub), payload u32 n + text
  CMDACK  0x20  a = command id, b = status (0 ok, 1 unknown command, 2 refused); payload u32 n (picture) + text
PC -> console
  SUB     0x1c  a = channels (1 records, 2 trace, 4 log), b = keep every b-th record (0 or 1: all); answered with LIVEDEF
  GRAB    0x1d  a = request id: the client sends the displayed picture as a STILL (its head's reserved word = the id)
  CMD     0x1e  a = command id, b = code (CMD_*); payload: step u32 count; warp text id; option text "name=value";
                pad u32 buttons, i8 stick x, i8 stick y, u8 lt, u8 rt, u32 pictures
"""
import struct

REC, LIVEDEF, TRACE, CMDACK = 0x0d, 0x0e, 0x0f, 0x20
SUB, GRAB, CMD = 0x1c, 0x1d, 0x1e
CH_REC, CH_TRACE, CH_LOG = 1, 2, 4
CMD_PAUSE, CMD_RESUME, CMD_STEP, CMD_WARP, CMD_OPTION, CMD_PAD = 1, 2, 3, 4, 5, 6
CMD_CODES = {'pause': CMD_PAUSE, 'resume': CMD_RESUME, 'step': CMD_STEP, 'warp': CMD_WARP, 'option': CMD_OPTION,
             'pad': CMD_PAD}
LOG_LEVELS = {1: 'info', 2: 'warn', 3: 'err', 4: 'stub'}
PAD_BITS = {'A': 1, 'B': 2, 'X': 4, 'Y': 8, 'START': 16, 'UP': 32, 'DOWN': 64, 'LEFT': 128, 'RIGHT': 256}
REC_F_POS = 1


def parse_recs(payload, count):
    """[(n, phases_us list, counters list, room or None, pos or None)]; stops at a malformed record."""
    out, o = [], 0
    for _ in range(count):
        if o + 8 > len(payload):
            break
        n, flags, nph, nctr = struct.unpack_from('<IHBB', payload, o)
        o += 8
        need = 4 * (nph + nctr) + (16 if flags & REC_F_POS else 0)
        if o + need > len(payload):
            break
        ph = list(struct.unpack_from('<%dI' % nph, payload, o))
        o += 4 * nph
        ctr = list(struct.unpack_from('<%di' % nctr, payload, o))
        o += 4 * nctr
        room = pos = None
        if flags & REC_F_POS:
            room, x, y, z = struct.unpack_from('<Ifff', payload, o)
            o += 16
            pos = [round(x, 3), round(y, 3), round(z, 3)]
        out.append((n, ph, ctr, room, pos))
    return out


def pack_recs(recs):
    """The inverse (the simulated console and the tests)."""
    b = bytearray()
    for n, ph, ctr, room, pos in recs:
        flags = REC_F_POS if pos is not None else 0
        b += struct.pack('<IHBB', n, flags, len(ph), len(ctr)) + struct.pack('<%dI' % len(ph), *ph)
        b += struct.pack('<%di' % len(ctr), *ctr)
        if pos is not None:
            b += struct.pack('<Ifff', room or 0, *pos)
    return bytes(b)


def parse_trace(payload, count):
    out, o = [], 0
    for _ in range(count):
        if o + 8 > len(payload):
            break
        n, d, lane, ln = struct.unpack_from('<IHBB', payload, o)
        o += 8
        out.append((n, d, lane, payload[o:o + ln].decode('utf-8', 'replace')))
        o += ln
    return out


def pack_trace(events):
    b = bytearray()
    for n, d, lane, label in events:
        lb = label.encode('utf-8')[:255]
        b += struct.pack('<IHBB', n, d, lane, len(lb)) + lb
    return bytes(b)


def parse_def(text):
    """The program's declaration, one item a line:
         program <name>                         rate <pictures a second>
         phase <key> <budget us or -> <total|-> <label...>
         ctr <key> <guard 0|1> <group> <label...>
         lane <id> <name...>
         preset <id> <group> <label...>
         option <name> bool <0|1> <label...> | int <min> <max> <value> <label...> | enum <a,b,c> <value> <label...>
       Unknown words are skipped (a newer client may declare more)."""
    d = {'program': '', 'rate': 60, 'phases': [], 'counters': [], 'lanes': {}, 'presets': [], 'options': []}
    for line in text.splitlines():
        w = line.split()
        if not w:
            continue
        k = w[0]
        try:
            if k == 'program' and len(w) > 1:
                d['program'] = ' '.join(w[1:])
            elif k == 'rate' and len(w) > 1:
                d['rate'] = float(w[1])
            elif k == 'phase' and len(w) >= 4:
                ph = {'key': w[1], 'label': ' '.join(w[4:]) or w[1]}
                if w[2] != '-':
                    ph['budget'] = round(int(w[2]) / 1000.0, 3)
                if w[3] == 'total':
                    ph['total'] = True
                d['phases'].append(ph)
            elif k == 'ctr' and len(w) >= 4:
                d['counters'].append({'key': w[1], 'guard': w[2] == '1', 'group': w[3], 'name': ' '.join(w[4:]) or w[1]})
            elif k == 'lane' and len(w) >= 3:
                d['lanes'][int(w[1])] = ' '.join(w[2:])
            elif k == 'preset' and len(w) >= 3:
                d['presets'].append({'id': w[1], 'group': w[2], 'label': ' '.join(w[3:]) or w[1]})
            elif k == 'option' and len(w) >= 4:
                name, typ = w[1], w[2]
                if typ == 'bool':
                    d['options'].append({'name': name, 'type': 'bool', 'value': w[3] == '1', 'label': ' '.join(w[4:]) or name})
                elif typ == 'int' and len(w) >= 6:
                    d['options'].append({'name': name, 'type': 'int', 'min': int(w[3]), 'max': int(w[4]),
                                         'value': int(w[5]), 'label': ' '.join(w[6:]) or name})
                elif typ == 'enum' and len(w) >= 5:
                    d['options'].append({'name': name, 'type': 'enum', 'values': w[3].split(','), 'value': w[4],
                                         'label': ' '.join(w[5:]) or name})
        except (ValueError, IndexError):
            continue
    return d


def pack_cmd_payload(c):
    """(code, payload bytes) for a /v0/command body, or raises ValueError."""
    cmd = c.get('cmd')
    if cmd == 'pause':
        return CMD_PAUSE, b''
    if cmd == 'resume':
        return CMD_RESUME, b''
    if cmd == 'step':
        return CMD_STEP, struct.pack('<I', max(1, min(3600, int(c.get('count') or 1))))
    if cmd == 'warp':
        return CMD_WARP, str(c['preset']).encode('utf-8')[:200]
    if cmd == 'option':
        v = c['value']
        v = '1' if v is True else '0' if v is False else str(v)
        return CMD_OPTION, ('%s=%s' % (c['name'], v)).encode('utf-8')[:200]
    if cmd == 'pad':
        st = c.get('state') or ((c.get('steps') or [{}])[0])
        bits = 0
        for b in st.get('buttons') or []:
            bits |= PAD_BITS.get(b, 0)
        sx, sy = (st.get('stick') or [0, 0])[:2]
        frames = int(sum(int(s.get('frames') or 0) for s in c.get('steps') or [])) if c.get('steps') else 0
        return CMD_PAD, struct.pack('<IbbBBI', bits, int(max(-1, min(1, sx)) * 127), int(max(-1, min(1, sy)) * 127),
                                    int(max(0, min(1, st.get('lt') or 0)) * 255), int(max(0, min(1, st.get('rt') or 0)) * 255),
                                    frames)
    raise ValueError('unknown command %r' % cmd)
