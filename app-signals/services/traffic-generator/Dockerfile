# 트래픽 생성기 + 장애 시나리오 클라이언트. 계측하지 않는다(Application Signals 서비스 맵에 나오지 않게).
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY traffic-generator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY traffic-generator/*.py ./

ENV PYTHONUNBUFFERED=1
USER 65534
CMD ["python", "traffic.py"]
