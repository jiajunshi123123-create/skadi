.PHONY: demo

# 只启动演示栈。不会读取仓库根目录的 .env，也不会拉起生产 compose。
demo:
	test -f config/demo.env || cp config/demo.env.example config/demo.env
	docker compose -f docker-compose.demo.yml up --build
