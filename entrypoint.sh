#!/bin/sh
# Both process groups run this image. Schema changes happen once per deploy via
# the release command, so the app and the worker never race to migrate — they
# just exec their command.
set -e

if [ "$RUN_MIGRATIONS" = "1" ]; then
    echo "running migrations..."
    python -m deskhand.migrate
    # Seed only into an empty database. A demo that wiped its tickets every
    # time a machine restarted would be a confusing demo, and a deploy that
    # silently discarded whatever a visitor was in the middle of would be
    # worse.
    python -m deskhand.seed --if-empty
fi

exec "$@"
