"""gunicorn 설정.

Application Signals(ADOT Python)는 pre-fork 서버에서 워커마다 계측을 초기화해야 합니다.
post_fork 훅 + OTEL_AWS_PYTHON_DEFER_TO_WORKERS_ENABLED=true (Deployment 환경 변수) 조합이 AWS 문서의 방법입니다:
https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Application-Signals-Enable-Troubleshoot.html
장애 시나리오 상태를 Pod 안에서 하나로 두려고 워커는 1개, 동시성은 스레드로 냅니다.
"""
import os

bind = "0.0.0.0:8080"
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "16"))
timeout = 30
accesslog = "-"


def post_fork(server, worker):
    try:
        from opentelemetry.instrumentation.auto_instrumentation import sitecustomize  # noqa: F401
    except ImportError:
        # 계측이 주입되지 않은 환경(주석 없는 배포, 로컬 실행)
        pass
