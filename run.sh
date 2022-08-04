#!/bin/bash

# Get to a predictable directory, the directory of this script
cd "$(dirname "$0")" || exit

while true; do
    git pull
    pip install -r requirements.txt
    python3 -m remind
    (($? != 42)) && break

    echo '==================================================================='
    echo '=                       Restarting                                ='
    echo '==================================================================='
done
