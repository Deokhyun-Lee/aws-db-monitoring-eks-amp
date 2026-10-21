#!/usr/bin/env python3
"""앱 → DB까지 이어 보는 RCA.

Application Signals의 골든 지표·의존성 지표에서 시작해, 가장 크게 나빠진 의존성을 따라 하위 서비스로 내려가고,
경로 위 DB 의존성은 트레이스(Transaction Search)의 느린 문장과 DB 엔진 지표(AMP, 없으면 CloudWatch)로 확인한다.

예)
  python rca.py --environment eks:integrated-db-monitoring-eks/shop --service order-api --minutes 15
  python rca.py --environment eks:… --service order-api --operation "POST /orders" \\
      --start 2026-10-22T05:10:00Z --end 2026-10-22T05:20:00Z --amp-workspace-id ws-… --output report.md

필요한 권한(읽기 전용): cloudwatch:GetMetricData, application-signals:ListServices·ListServiceDependencies,
logs:StartQuery/GetQueryResults(aws/spans), xray:GetTraceSummaries, rds:DescribeDBClusters/DescribeDBInstances,
elasticache:DescribeReplicationGroups, aps:QueryMetrics
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analysis import Window, Windows, diagnose, walk  # noqa: E402
from report import markdown, to_json  # noqa: E402


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def windows_from(args):
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    if args.start:
        start = parse_time(args.start)
        end = parse_time(args.end) if args.end else now
    else:
        end = now
        start = end - dt.timedelta(minutes=args.minutes)
    length = end - start
    if length.total_seconds() < 120:
        sys.exit("사고 구간은 2분 이상이어야 합니다")
    base_len = dt.timedelta(minutes=args.baseline_minutes) if args.baseline_minutes else length
    return Windows(baseline=Window(start - base_len, start), incident=Window(start, end))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--environment", required=True, help="Application Signals Environment (예: eks:<클러스터>/shop)")
    ap.add_argument("--service", required=True, help="시작 서비스 (예: order-api)")
    ap.add_argument("--operation", help='시작 오퍼레이션 (예: "POST /orders"). 없으면 서비스 전체')
    ap.add_argument("--start", help="사고 구간 시작 (ISO 8601, UTC)")
    ap.add_argument("--end", help="사고 구간 끝 (기본: 지금)")
    ap.add_argument("--minutes", type=int, default=15, help="--start가 없을 때 최근 N분을 사고 구간으로 (기본 15)")
    ap.add_argument("--baseline-minutes", type=int, help="기준 구간 길이 (기본: 사고 구간과 같은 길이, 바로 앞)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    ap.add_argument("--amp-workspace-id", default=os.environ.get("AMP_WORKSPACE_ID"))
    ap.add_argument("--instance-label", default="db_instance", help="엔진 지표의 DB 인스턴스 라벨 (기본 db_instance)")
    ap.add_argument("--max-depth", type=int, default=3, help="하위 서비스로 내려갈 최대 깊이")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--output", help="결과 파일 (기본: 표준 출력)")
    args = ap.parse_args(argv)
    if not args.region:
        sys.exit("--region 또는 AWS_REGION을 지정하세요")

    from collector import AwsCollector  # boto3는 실행할 때만 필요

    windows = windows_from(args)
    collector = AwsCollector(args.region, amp_workspace_id=args.amp_workspace_id, instance_label=args.instance_label)
    root = walk(collector, args.environment, args.service, args.operation, windows, max_depth=args.max_depth)
    hypotheses = diagnose(root, windows)
    render = markdown if args.format == "markdown" else to_json
    out = render(root, hypotheses, windows, collector.sources)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"saved {args.output}: {hypotheses[0].title} [{hypotheses[0].confidence}]")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
