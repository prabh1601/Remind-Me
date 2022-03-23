#!/bin/bash

# Get to a predictable directory, the directory of this script
cd "$(dirname "$0")"

while true; do

    pip install -r requirements.txt
    python3 -m remind
    (( $? != 42 )) && break

    echo '==================================================================='
    echo '=                       Restarting                                ='
    echo '==================================================================='
done
