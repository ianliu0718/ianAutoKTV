def post_worker_init(worker):
    # Start the shared NAS sync only after the worker process has loaded the WSGI app.
    from render_app import start_qnap_sync_after_worker_start

    start_qnap_sync_after_worker_start()
