# 빌드 컨텍스트: app-signals/services (공용 모듈 python-common/을 함께 넣음)
#   docker build -f order-api/Dockerfile .
# ADOT Python SDK는 이미지에 넣지 않습니다. EKS에서 CloudWatch Observability add-on이 init 컨테이너로 주입합니다.
# 주입 방식이 glibc 기반 이미지를 전제하므로 alpine이 아닌 slim을 씁니다.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY order-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY python-common/*.py ./
COPY order-api/*.py ./

# Application Signals 주입 시 PYTHONPATH가 없으면 앱이 시작되지 않는 알려진 문제가 있어 작업 디렉터리를 지정합니다
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
USER 65534
EXPOSE 8080
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
