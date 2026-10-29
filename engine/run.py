"""python3 -m engine.run --farm cheese [--audit] [--json out.json]"""
from __future__ import annotations

import argparse
import json
from datetime import date

from .benchmarks import BENCHMARKS
from .generate import SPECS, TODAY, build_farm
from .privacy import usable_benchmarks
from .render import report, selection_to_dict
from .scoring import prioritize
from .signals import detect_all


def recommend(key: str, today: date = TODAY, snoozed=None):
    farm = build_farm(key, today)
    bench = usable_benchmarks(BENCHMARKS)
    return farm, prioritize(detect_all(farm, bench), farm, snoozed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Три дела на сегодня для фермера (синтетические данные)")
    parser.add_argument("--farm", choices=list(SPECS) + ["all"], default="cheese")
    parser.add_argument("--audit", action="store_true", help="показать отложенные, слабые и заблокированные действия")
    parser.add_argument("--json", metavar="FILE", help="сохранить результат в JSON")
    parser.add_argument("--today", help="дата в формате ГГГГ-ММ-ДД (по умолчанию дата демо)")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else TODAY
    keys = list(SPECS) if args.farm == "all" else [args.farm]
    dumps = []
    for key in keys:
        farm, sel = recommend(key, today)
        print(report(farm, sel, audit=args.audit))
        dumps.append(selection_to_dict(farm, sel))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(dumps if len(dumps) > 1 else dumps[0], fh, ensure_ascii=False, indent=2)
        print(f"JSON сохранён: {args.json}")


if __name__ == "__main__":
    main()
