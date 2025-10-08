#!/bin/bash
set -e; set -o pipefail; set -m
[ "${DEBUG^^}" != 'TRUE' ] || set -x

# do it explicitely
set -x

my_dir=$(realpath $(dirname "$0"))

# folder with built binaries/libs/docs/data/... files from tf-dev-sandbox container
export BUILD_ROOT=${BUILD_ROOT:-'/buildroot'}

rm -rf $BUILD_ROOT || /bin/true
