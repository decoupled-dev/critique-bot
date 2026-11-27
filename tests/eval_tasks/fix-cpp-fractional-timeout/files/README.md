# svcconf

Loader for the service's `key = value` config files.

| key       | meaning                                   | rules                                  |
|-----------|-------------------------------------------|----------------------------------------|
| `host`    | address to bind                           | non-empty                              |
| `port`    | TCP port                                  | whole number, 1-65535                  |
| `timeout` | request timeout in seconds                | positive, fractions allowed (`1.5`), stored in milliseconds |
| `retries` | retry attempts                            | whole number, 0-10                     |

Lines starting with `#` are comments. Bad values are reported with their line
number and the default is kept.

    make          # builds ./svcconf
    make test     # builds and runs tests/test_config
