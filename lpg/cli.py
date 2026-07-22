"""Command-line entry point for the private LPG ingestion workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .platts_excel import (
    build_probe_workbook,
    build_scope_workbooks,
    parse_workbook,
    write_staging,
)
from .service import LpgService
from .workflow import LpgRefreshWorkflow


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fincept LPG licensed-data workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build private daily Add-in workbooks")
    build.add_argument("--scope", choices=("asia", "overnight", "all"), default="all")
    build.add_argument("--force", action="store_true")

    probe = sub.add_parser("build-probe", help="build one live Add-in validation formula")
    probe.add_argument("--symbol", default="PMAAV00")

    refresh = sub.add_parser("refresh", help="refresh Excel, stage, and import")
    refresh.add_argument(
        "--scope",
        choices=("asia", "overnight", "all", "news", "history", "curves", "moc"),
        default="all",
    )
    refresh.add_argument("--timeout", type=int, default=240)

    parse = sub.add_parser("parse", help="parse an already-saved Add-in workbook")
    parse.add_argument("workbook", type=Path)
    parse.add_argument("--no-import", action="store_true")

    ingest = sub.add_parser("import", help="import an atomic staging JSON file")
    ingest.add_argument("staging", type=Path)

    vessel_import = sub.add_parser(
        "import-vessels", help="import a historical vessel port-call CSV snapshot",
    )
    vessel_import.add_argument("port_calls", type=Path)
    vessel_import.add_argument("--fleet-group", default="reference_fleet")

    backfill = sub.add_parser("build-backfill", help="build resumable yearly backfill workbooks")
    backfill.add_argument("--start-year", type=int, required=True)
    backfill.add_argument("--end-year", type=int, required=True)
    backfill.add_argument("--scope", choices=("asia", "overnight", "all"), default="all")
    backfill.add_argument(
        "--symbol", action="append",
        help="configured Platts symbol or candidate id; repeat for a subset",
    )
    backfill.add_argument("--batch-size", type=int, default=1)
    backfill.add_argument("--force", action="store_true")

    history = sub.add_parser(
        "refresh-history", help="build, refresh, stage, and import yearly history batches",
    )
    history.add_argument("--start-year", type=int)
    history.add_argument("--end-year", type=int)
    history.add_argument("--scope", choices=("asia", "overnight", "all"), default="all")
    history.add_argument(
        "--symbol", action="append",
        help="configured Platts symbol or candidate id; repeat for a subset",
    )
    history.add_argument("--batch-size", type=int, default=1)
    history.add_argument("--timeout", type=int, default=240)
    history.add_argument("--force", action="store_true")

    build_curves = sub.add_parser("build-curves", help="build isolated FC CurveData workbooks")
    build_curves.add_argument("--scope", choices=("asia", "overnight", "all"), default="all")
    build_curves.add_argument("--curve", action="append", help="curve candidate id or FC code")
    build_curves.add_argument("--batch-size", type=int, default=1)
    build_curves.add_argument("--force", action="store_true")

    curves = sub.add_parser("refresh-curves", help="refresh, stage, and import FC curves")
    curves.add_argument("--scope", choices=("asia", "overnight", "all"), default="all")
    curves.add_argument("--curve", action="append", help="curve candidate id or FC code")
    curves.add_argument("--batch-size", type=int, default=1)
    curves.add_argument("--timeout", type=int, default=240)
    curves.add_argument("--force", action="store_true")

    build_moc = sub.add_parser("build-moc", help="build isolated Platts eWindow workbook")
    build_moc.add_argument("--scope", choices=("asia", "overnight", "all"), default="all")
    build_moc.add_argument("--force", action="store_true")

    moc = sub.add_parser("refresh-moc", help="refresh, stage, and import Platts eWindow MOC")
    moc.add_argument("--scope", choices=("asia", "overnight", "all"), default="all")
    moc.add_argument("--timeout", type=int, default=240)
    moc.add_argument("--force", action="store_true")

    sub.add_parser("status", help="show the local entitlement and source-health matrix")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = LpgService()
    workflow = LpgRefreshWorkflow(service=service)
    if args.command == "build":
        paths = build_scope_workbooks(scope=args.scope, force=args.force)
        payload = {"state": "succeeded", "workbooks": [str(path) for path in paths]}
    elif args.command == "build-probe":
        path = build_probe_workbook(symbol=args.symbol, force=True)
        payload = {"state": "succeeded", "workbook": str(path), "symbol": args.symbol.upper()}
    elif args.command == "refresh":
        payload = workflow.refresh(args.scope, timeout_seconds=args.timeout)
    elif args.command == "parse":
        parsed = parse_workbook(args.workbook)
        staged = write_staging(parsed)
        payload = {"parsed": {"status": parsed.get("status"),
                              "purpose": parsed.get("purpose"),
                              "year": parsed.get("year"),
                              "records": len(parsed.get("records") or []),
                              "entitlements": len(parsed.get("entitlement_results") or [])},
                   "staging": str(staged) if staged else None}
        if staged and not args.no_import:
            payload["import"] = service.import_platts_staging(staged)
    elif args.command == "import":
        payload = service.import_platts_staging(args.staging)
    elif args.command == "import-vessels":
        payload = service.import_vessel_port_calls(
            args.port_calls, fleet_group=args.fleet_group,
        )
    elif args.command == "build-backfill":
        payload = workflow.build_backfill(
            args.start_year, args.end_year, scope=args.scope, symbols=args.symbol,
            batch_size=args.batch_size, force=args.force,
        )
    elif args.command == "refresh-history":
        payload = workflow.refresh_history(
            start_year=args.start_year, end_year=args.end_year, scope=args.scope,
            symbols=args.symbol, batch_size=args.batch_size,
            timeout_seconds=args.timeout, force=args.force,
        )
    elif args.command == "build-curves":
        payload = workflow.build_curves(
            scope=args.scope, curve_ids=args.curve,
            batch_size=args.batch_size, force=args.force,
        )
    elif args.command == "refresh-curves":
        payload = workflow.refresh_curves(
            scope=args.scope, curve_ids=args.curve,
            batch_size=args.batch_size, timeout_seconds=args.timeout, force=args.force,
        )
    elif args.command == "build-moc":
        payload = workflow.build_moc(scope=args.scope, force=args.force)
    elif args.command == "refresh-moc":
        payload = workflow.refresh_moc(
            scope=args.scope, timeout_seconds=args.timeout, force=args.force,
        )
    else:
        payload = service.status()
    _print(payload)
    state = str(payload.get("state") or payload.get("status") or "succeeded")
    return 0 if state in {"succeeded", "success", "partial", "deferred"} else 1


if __name__ == "__main__":
    sys.exit(main())
