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

SConscript('openstack/nova_contrail_vif/SConscript')
SConscript('openstack/neutron_plugin/SConscript')
SConscript('openstack/heat_plugin/SConscript')

if GetOption("describe-tests"):
    rules.DescribeTests(env, COMMAND_LINE_TARGETS)
    Exit(0)

if GetOption("describe-aliases"):
    rules.DescribeAliases()
    Exit(0)
