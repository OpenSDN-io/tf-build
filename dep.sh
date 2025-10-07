#!/bin/bash
set -e; set -o pipefail; set -m
[ "${DEBUG^^}" != 'TRUE' ] || set -x

# do it explicitely
set -x

# install BuildRequires here to be able to customize for each release (tf-dev-env doesn't have branches)

# version 2.17.1 of cassandra-cpp-driver has bug with callbacks and doesn't work properly for us
dnf install -y cassandra-cpp-driver cassandra-cpp-driver-devel \
  libcurl-devel libcurl-minimal librdkafka-devel libzookeeper-devel \
  libstdc++-devel libxml2-devel lz4-devel protobuf protobuf-compiler protobuf-devel \
  systemd-units tbb-devel tokyocabinet-devel zlib-devel libcmocka-devel libxslt-devel

# dpdk
dnf install -y numactl-devel libnl3-devel libpcap libpcap-devel "rdma-core-devel-47mlnx1-1.47329"

# webui
dnf install -y "nodejs-1:16.20.2" "npm-1:8.19.4"

# python deps
python3 -m pip install scons lxml "Sphinx<7.3.0" requests setuptools "pyyaml<6"
