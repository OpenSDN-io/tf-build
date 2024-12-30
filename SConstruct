# -*- mode: python; -*-

#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#

# repository root directory
import os
import sys

sys.path.append('tools/build')

import rules
conf = Configure(DefaultEnvironment(ENV = os.environ))
env = rules.SetupBuildEnvironment(conf)

SConscript(dirs=['src/contrail-common', 'controller', 'vrouter'])

if os.path.exists('openstack/nova_contrail_vif/SConscript'):
    SConscript('openstack/nova_contrail_vif/SConscript',
               variant_dir='build/noarch/nova_contrail_vif')

if os.path.exists('openstack/neutron_plugin/SConscript'):
    SConscript('openstack/neutron_plugin/SConscript',
               variant_dir='build/noarch/neutron_plugin')

if GetOption("describe-tests"):
    rules.DescribeTests(env, COMMAND_LINE_TARGETS)
    Exit(0)

if GetOption("describe-aliases"):
    rules.DescribeAliases()
    Exit(0)
