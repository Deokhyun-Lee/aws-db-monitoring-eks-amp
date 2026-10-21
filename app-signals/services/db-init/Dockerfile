# 샘플 서비스용 DB 객체 생성 Job. 계측하지 않는다.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY db-init/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY db-init/db_init.py ./

ENV PYTHONUNBUFFERED=1
USER 65534
CMD ["python", "db_init.py"]
