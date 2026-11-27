# taskq

A tiny to-do list kept in a JSON file.

    python3 -m taskq.cli --file todo.json add "Buy milk"
    python3 -m taskq.cli --file todo.json list [--all]
    python3 -m taskq.cli --file todo.json done 1

Tests: `python3 -m unittest discover -s tests`
