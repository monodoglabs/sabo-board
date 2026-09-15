#!/usr/bin/env python3
"""
SABO SEATGEN — the one-truth seat system for the SABO board.

What this is:
  seats.json  = the SINGLE SOURCE OF TRUTH: every tuneable element's baked
                 seat (dx/dy/s/r), which carriers carry it, and every canon
                 asset URL. Lives in the repo (api.github.com = always fresh).
  seatgen.py  = the machine that keeps the carriers honest:
                 extract()  - read baked seats out of a carrier file
                 stamp()    - write a seat into a carrier (rule-block rewrite)
                 stamp_asset() - swap an asset in BOTH forms (visible img URL
                                 + preload-gate P+'name' concat)
                 canon_check()  - diff seats across carriers + vs the spec;
                                  FAILS LOUD if any carrier drifted
                 verify_live() - pull the live manifest from the repo API and
                                 hash-check every carrier file vs the local
                                 build tree (kills stale-copy rebuilds)
                 bake_push()   - apply her newest SeatTuning push to the spec
                                  (newest supersedes), ready for stamping

Rules burned into this file (from real incidents):
  - Never hand-edit a carrier to bake a seat; run stamp() for EVERY carrier.
  - Never string-replace structural tags; only rewrite inside CSS rule blocks
    and asset-URL patterns.
  - Bakes: update seats.json -> stamp all carriers -> canon_check MUST pass
    -> headless proof -> upload -> manifest bump -> wipe same-pass.
"""
import json, re, sys, hashlib

# ---------------------------------------------------------------- utilities

def load_spec(path='seats.json'):
    with open(path) as f:
        return json.load(f)

def extract(html, selector):
    """Return the baked individual transform properties for a CSS rule."""
    m = re.search(re.escape(selector) + r'\{([^}]*)\}', html)
    if not m:
        return None
    body = m.group(1)
    out = {}
    t = re.search(r'(?:^|;)\s*translate:\s*([^;]+)', body)
    if t: out['translate'] = t.group(1).strip()
    s = re.search(r'(?:^|;)\s*scale:\s*([^;]+)', body)
    if s: out['scale'] = s.group(1).strip()
    r = re.search(r'(?:^|;)\s*rotate:\s*([^;]+)', body)
    if r: out['rotate'] = r.group(1).strip()
    top = re.search(r'(?:^|;)\s*top:\s*([^;]+)', body)
    if top: out['top'] = top.group(1).strip()
    return out if out else {}

def extract_all(html, spec):
    """Extract every element's seat from a carrier. Returns {el: props}."""
    out = {}
    for el, e in spec['elements'].items():
        got = extract(html, e['selector'])
        if got:
            out[el] = got
    return out

def stamp(html, selector, seat):
    """Write dx/dy/s/r into a rule as individual properties (engine math)."""
    m = re.search(re.escape(selector) + r'\{([^}]*)\}', html)
    if not m:
        raise SystemExit('STAMP FAIL: selector %s not found' % selector)
    body = m.group(1)
    for prop in ('translate', 'scale', 'rotate'):
        body = re.sub(r'(?:^|;)\s*' + prop + r':\s*[^;]+;?', '', body)
    add = ''
    if seat.get('dx') or seat.get('dy'):
        add += 'translate:%dpx %dpx;' % (seat.get('dx', 0), seat.get('dy', 0))
    if seat.get('s') and seat['s'] != 1:
        add += 'scale:%s;' % seat['s']
    if seat.get('r'):
        add += 'rotate:%ddeg;' % seat['r']
    if add:
        body = body.rstrip().rstrip(';') + ';' + add
    else:
        body = body.rstrip().rstrip(';')
    return html[:m.start(1)] + body + html[m.end(1):]

def stamp_asset(html, pattern, new_url, new_gate_name):
    """Swap an asset in BOTH forms. pattern = regex matching any version's
    fragment; new_url = full URL; new_gate_name = hash_name.png for gates.
    Returns (html, n_visible, n_gate)."""
    rx = re.compile(pattern)
    hits = list(rx.finditer(html))
    if not hits:
        return html, 0, 0
    new_hash_name = new_url.rsplit('/', 1)[-1]
    # visible full-URL form
    html2 = re.sub(rx, lambda m: (new_url if m.group(0).startswith('http')
                                  else "P+'" + new_gate_name + "'"), html)
    n_visible = len(re.findall(r'https?://[^\s"\'<>]*' + re.escape(new_hash_name), html2))
    n_gate = len(re.findall(re.escape("P+'" + new_gate_name + "'"), html2))
    leftovers = [h for h in hits if h.group(0) not in (new_url, "P+'" + new_gate_name + "'")]
    if leftovers:
        # anything still matching the pattern that isn't the new asset = stale ref
        remaining = re.findall(rx, html2)
        raise SystemExit('STAMP_ASSET FAIL: %d stale refs remain' % len(remaining))
    return html2, n_visible, n_gate

# ------------------------------------------------------------- canon check

def canon_check(files, spec):
    """files: {carrier: html}. Diffs every element across its carriers and
    against the spec. Prints a table; returns True if all clean."""
    ok = True
    def want_props(seat):
        w = {}
        if seat.get('dx') or seat.get('dy'):
            w['translate'] = '%dpx %dpx' % (seat.get('dx', 0), seat.get('dy', 0))
        if seat.get('s') and seat['s'] != 1:
            w['scale'] = str(seat['s'])
        if seat.get('r'):
            w['rotate'] = '%ddeg' % seat['r']
        return w

    print('%-12s %-22s %-26s %s' % ('ELEMENT', 'SPEC SEAT', 'CARRIERS', 'VERDICT'))
    for el, e in sorted(spec['elements'].items()):
        if e.get('seat_kind') == 'inline':
            continue  # handled by canon_check_inline
        def norm(props):
            n = {}
            if 'translate' in props:
                n['translate'] = props['translate']
            if 'scale' in props:
                n['scale'] = float(props['scale'])
            if 'rotate' in props:
                n['rotate'] = int(float(props['rotate'].replace('deg', '')))
            return json.dumps(n, sort_keys=True)
        want = norm(want_props(e['seat']))
        got = {}
        for c in e['carriers']:
            if c not in files:
                continue
            g = extract(files[c], e['selector'])
            if g is None:
                got[c] = 'MISSING'
            else:
                got[c] = norm({k: g[k] for k in ('translate', 'scale', 'rotate') if k in g})
        live_vals = got
        verdict = 'ok'
        if 'MISSING' in live_vals.values():
            ok = False; verdict = 'MISSING: ' + ','.join(c for c, v in live_vals.items() if v == 'MISSING')
        elif set(live_vals.values()) != {want}:
            ok = False
            verdict = 'DRIFT vs SPEC: ' + ' | '.join('%s=%s' % (c, v) for c, v in live_vals.items()
                                                    if v != want) + ' || want=' + want
        print('%-12s %-22s %-26s %s' % (el, want if want != '{}' else '(base only)',
                                        ','.join(sorted(got)), verdict))
    # asset refs
    for name, a in sorted(spec.get('assets', {}).items()):
        got = {}
        for c in a['carriers']:
            if c in files:
                n_vis = len(re.findall(re.escape(a['url'].rsplit('/', 1)[-1]), files[c]))
                n_gate = len(re.findall(re.escape("P+'" + a['gate'] + "'"), files[c]))
                exp_gate = a.get('gates', {}).get(c, 0)
                got[c] = 'vis:%d gate:%d' % (n_vis, n_gate)
                if n_vis < 1 or n_gate != exp_gate:
                    ok = False
        print('%-12s %-10s %-34s %s' % ('asset:' + name, a['url'][-24:],
              ','.join(sorted(got)), 'ok' if ok else 'CHECK'))
    return ok

# ------------------------------------------------------------ live verify

def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]

def verify_live(files_live, files_local):
    """files_live: {carrier: html downloaded from the manifest URLs}.
    files_local: {carrier: html in the build tree}. Fails on any mismatch."""
    ok = True
    for c, live in files_live.items():
        local = files_local.get(c)
        if local is None:
            print('%-10s LOCAL MISSING' % c); ok = False; continue
        if sha(live) != sha(local):
            print('%-10s HASH MISMATCH live=%s local=%s — build tree stale, re-download'
                  % (c, sha(live), sha(local))); ok = False
        else:
            print('%-10s live==local (%s)' % (c, sha(live)))
    return ok

# ---------------------------------------------------------------- bake

def seat_from_delta(d):
    """Tune-engine delta -> spec seat (translate/rotate/scale individual
    properties, composing over base CSS transforms)."""
    return {'dx': d.get('dx', 0), 'dy': d.get('dy', 0),
            's': d.get('s', 1) if d.get('s') not in (None, 0) else 1,
            'r': d.get('r', 0)}

def bake_push(spec, push_deltas, override_carriers=None):
    """Apply her pushed deltas to the spec (newest supersedes). Only updates
    elements the spec knows; anything else is reported as UNKNOWN."""
    unknown = [d['id'] for d in push_deltas if d['id'] not in spec['elements']]
    for d in push_deltas:
        if d['id'] in spec['elements']:
            spec['elements'][d['id']]['seat'] = seat_from_delta(d)
            if override_carriers and d['id'] in override_carriers:
                spec['elements'][d['id']]['carriers'] = override_carriers[d['id']]
    return unknown

def canon_check_inline(files, spec):
    ok = True
    for el, e in sorted(spec['elements'].items()):
        if e.get('seat_kind') != 'inline':
            continue
        want = json.dumps(e['seat'], sort_keys=True)
        for c in e['carriers']:
            m = re.search(r"id='%s'[^>]*style='([^']*)'" % el, files[c])
            if not m:
                print('%-12s %-10s MISSING inline seat in %s' % (el, '', c)); ok = False; continue
            left = re.search(r'left:(-?\d+)px', m.group(1)); top = re.search(r'top:(-?\d+)px', m.group(1))
            st = re.search(r"<div class='cstack' style='([^']*)'", files[c][m.end():m.end()+400])
            sc = re.search(r'scale\((\d[\d.]*)\)', st.group(1)) if st else None
            rot = re.search(r'rotate\((-?\d+)deg\)', st.group(1)) if st else None
            got = json.dumps({'left': int(left.group(1)) if left else None,
                              'top': int(top.group(1)) if top else None,
                              's': float(sc.group(1)) if sc else 1,
                              'r': int(rot.group(1)) if rot else 0}, sort_keys=True)
            if got != want:
                ok = False
                print('%-12s %-10s DRIFT in %s: live=%s want=%s' % (el, '', c, got, want))
            else:
                print('%-12s %-10s %-34s ok' % (el, want[:30], c))
    return ok

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'check'
    spec = load_spec(sys.argv[2] if len(sys.argv) > 2 else 'seats.json')
    if cmd == 'check':
        carriers = sorted({c for e in spec['elements'].values() for c in e['carriers']}
                          | set(spec.get('assets', {}).get('chit_background', {}).get('carriers', {})))
        files = {c: open(spec['carrier_files'][c]).read() for c in carriers}
        ok1 = canon_check(files, spec)
        print('--- inline piles ---')
        ok2 = canon_check_inline(files, spec)
        print('--- assets ---')
        for name, a in spec.get('assets', {}).items():
            for c in a['carriers']:
                nvis = len(re.findall(re.escape(a['url'].rsplit('/', 1)[-1]), files[c]))
                ngate = len(re.findall(re.escape("P+'" + a['gate'] + "'"), files[c]))
                exp = a['gates'].get(c, 0)
                status = 'ok' if (nvis >= 1 and ngate == exp) else 'FAIL'
                print('asset:%-16s %-9s visible:%d gate:%d (expect %d) %s' % (name, c, nvis, ngate, exp, status))
                if status == 'FAIL': ok2 = False
        print('CANON CHECK:', 'PASS' if (ok1 and ok2) else 'FAIL')
        sys.exit(0 if (ok1 and ok2) else 1)
    print('commands: check')
