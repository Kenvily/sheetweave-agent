"""Keep worker heartbeats alive while synchronous LLM/PDF requests wait."""

worker_class = "gthread"
threads = 2
timeout = 180
graceful_timeout = 180
