Extra VictoriaMetrics scrape jobs for the demo. Every `*.yml` here is a Prometheus scrape-config
file (`scrape_configs:` list) loaded by `../scrape.yml`; VM reloads it on SIGHUP or
`curl -X POST http://127.0.0.1:8429/-/reload`.

queue-sim (bead 1h9.17), running on the host:

    scrape_configs:
      - job_name: queue-sim
        scrape_interval: 5s
        static_configs:
          - targets: ["host.containers.internal:9108"]

or as a compose service in the `tn-demo` project (`name: tn-demo` network is shared when added
to `compose.yml` or an extra `-f` file): use `targets: ["queue-sim:9108"]`.
