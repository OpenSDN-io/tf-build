#!/bin/bash
set -e; set -o pipefail; set -m
[ "${DEBUG^^}" != 'TRUE' ] || set -x

# do it explicitely
set -x

my_dir=$(realpath $(dirname "$0"))
src_root=$(dirname $(dirname "$my_dir"))
buildroot="$src_root/buildroot"

rm -rf $buildroot || /bin/true
