"""Keep worker heartbeats alive while synchronous LLM/PDF requests wait."""

workers = 2
worker_class = "gthread"
threads = 2
timeout = 180
graceful_timeout = 180
