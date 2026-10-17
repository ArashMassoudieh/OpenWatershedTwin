#!/bin/bash
# Rock Creek live twin, AWS deploy: deploy/deploy_twin.sh RockCreek [--with-state] [--fresh] [--no-venv]
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_twin.sh" RockCreek "$@"
