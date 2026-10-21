# 빌드 컨텍스트: app-signals/services
#   docker build -f payment-service/Dockerfile .
# ADOT Node.js SDK는 이미지에 넣지 않습니다. EKS에서 CloudWatch Observability add-on이 주입합니다.
FROM public.ecr.aws/docker/library/node:22-slim

WORKDIR /app
COPY payment-service/package.json payment-service/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY payment-service/index.js ./

ENV NODE_ENV=production
USER node
EXPOSE 8080
CMD ["node", "index.js"]
