import json, glob, os, sys

D = r'D:\Temp\opencode'
DIRS = ('captcha_calib4', 'captcha_calib5', 'captcha_calib7')


def main(argv):
    target = argv[0] if argv else D
    rows = []
    for d in DIRS:
        for f in glob.glob(os.path.join(target, d, '*.json')):
            try:
                j = json.load(open(f, encoding='utf-8-sig'))
            except Exception:
                continue
            if j.get('verdict') != 'solved' or j.get('bx_true') is None:
                continue
            bt = j['bx_true']
            e0 = (j.get('edges') or [None])[0]
            silR = j.get('silR')
            hit = any(x[0] is not None and abs((e0 or 0) - x[0] - bt) <= 3
                      for x in [(silR,)]) if (e0 and silR) else None
            c0 = (j.get('cands') or [[None]])[0][0]
            rows.append((d, os.path.basename(f), bt, j.get('silL'), silR, e0,
                         'HIT' if hit else ('-' if hit is None else 'MISS'),
                         abs(c0 - bt) if c0 else None))
    print(f"{'dir':>8} {'file':>10} {'bx':>4} {'silL':>4} {'silR':>4} {'e0':>4} "
          f"{'e-silR':>7} {'c0':>4} {'d':>3}")
    for d, n, bt, sl, sr, e0, hit, d1 in rows:
        es = (e0 - sr) if (e0 and sr) else None
        print(f"{d:>8} {n:>10} {bt:>4} {str(sl):>4} {str(sr):>4} {str(e0):>4} "
              f"{str(es):>7} {(c0 if False else ''):>4} "
              f"{str(d1):>3} {hit}")
    ok = sum(1 for r in rows if r[6] == 'HIT')
    okc = sum(1 for r in rows if r[7] is not None and r[7] <= 1)
    print(f"\nsilR hit: {ok}/{len(rows)}  top1 cands(<=1): {okc}/{len(rows)}")


if __name__ == '__main__':
    main(sys.argv[1:])