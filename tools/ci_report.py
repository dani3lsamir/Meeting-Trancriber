"""Summarise self-test reports + app log as a GitHub check-run payload (CI helper).

    python tools/ci_report.py <head_sha> <out.json> selftest-*.json [--log app.log]
"""
import glob
import json
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    sha, out = args[0], Path(args[1])
    log = None
    if '--log' in args:
        i = args.index('--log')
        log = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
    files = sorted({f for pattern in args[2:] for f in glob.glob(pattern)})
    lines, all_ok = [], True
    for f in files:
        try:
            data = json.loads(Path(f).read_text(encoding='utf-8'))
        except Exception as exc:
            lines += [f'## {f}', f'unreadable: {exc}', '']
            all_ok = False
            continue
        ok = data.get('ok')
        lines += [f'## {f}: {"PASSED" if ok else "FAILED"}', '', '| check | result | detail |', '|---|---|---|']
        for c in data.get('checks', []):
            detail = str(c.get('detail', '')).replace('|', '/').replace('\n', ' ')[:300]
            lines.append(f'| {c["name"]} | {"PASS" if c["ok"] else "FAIL"} | {detail} |')
        if 'capture' in data:
            lines += ['', f'capture: `{json.dumps(data["capture"])[:600]}`']
        if 'devices' in data:
            lines += ['', f'devices: `{json.dumps(data["devices"])[:600]}`']
        lines.append('')
    if log and log.is_file():
        tail = log.read_text(encoding='utf-8', errors='replace').splitlines()[-80:]
        lines += ['## app.log (tail)', '```', *tail, '```']
    text = '\n'.join(lines)[:60000]
    payload = {'name': 'Self-test report', 'head_sha': sha, 'status': 'completed',
               'conclusion': 'success' if all_ok else 'neutral',
               'output': {'title': f'{len(files)} self-test reports', 'summary': f'{len(files)} reports', 'text': text}}
    out.write_text(json.dumps(payload), encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
