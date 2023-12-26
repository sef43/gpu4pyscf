#!/bin/bash

echo "PATH=${PATH}"

python3 setup.py bdist_wheel

auditwheel repair dist/gpu4pyscf*.whl
