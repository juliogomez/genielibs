import re
import os
import yaml
import inspect
import logging
import importlib
from collections import ChainMap
from copy import deepcopy
from collections import namedtuple

from robot.api.deco import keyword
from robot.libraries.BuiltIn import BuiltIn, RobotNotRunningError

from genie.abstract import Lookup

from ats.utils.objects import find, R
from ats.aetest import executer, reporter
from ats.datastructures.logic import Or
from ats.results import (Passed, Failed, Aborted, Errored,
                         Skipped, Blocked, Passx)

from genie.conf import Genie
from genie.conf.base import loader
from genie.conf.base import Testbed
from genie.harness.script import TestScript
from genie.harness.discovery import GenieScriptDiscover
from genie.harness.datafile.loader import TriggerdatafileLoader,\
                                          VerificationdatafileLoader

log = logging.getLogger(__name__)


class GenieRobot(object):
    '''Genie RobotFramework library'''

    # Need to maintain the testscript object
    ROBOT_LIBRARY_SCOPE = "GLOBAL"

    def __init__(self):
        # save builtin so we dont have to re-create then everytime
        self.builtin = BuiltIn()

        # Need to create a testscript
        try:
            # If pyATS, then call their use_testbed api, then convert
            self._pyats_testscript = self.builtin.get_library_instance(
                                    'ats.robot.pyATSRobot').testscript
        except RuntimeError:
            self._pyats_testscript = None
            # No pyATS
            pass
        except RobotNotRunningError:
           # For building doc
           return
        finally:
            self._genie_testscript = TestScript(Testscript)

        # Load Genie Datafiles (Trigger and Verification)
        self._load_genie_datafile()

    @property
    def testscript(self):
        try:
            return self._genie_testscript
        except Exception:
            return self._pyats_testscript

    @keyword('use genie testbed "${testbed}"')
    def genie_testbed(self, testbed):
        '''Create the genie testbed'''
        try:
            # If pyATS, then call their use_testbed api, then convert
            self.builtin.get_library_instance('ats.robot.pyATSRobot').\
                         use_testbed(testbed)
            testbed = self.builtin.get_library_instance('ats.robot.'\
                                                        'pyATSRobot').testbed
        except RuntimeError:
            # No pyATS
            self.testbed = loader.load(testbed)
        else:
            # Has pyATS, so converted and then save locally and also for pyATS
            self.testbed = Genie.init(testbed)
            self.builtin.get_library_instance('ats.robot.'\
                                              'pyATSRobot').testbed =\
                                              self.testbed
        self.testscript.parameters['testbed'] = self.testbed

    # Metaparser
    @keyword('parse "${parser:[^"]+}" on device "${device:[^"]+}"')
    def metaparser_on_device(self, parser, device):
        '''Call any `metaparser` parser and parse the device output.'''
        return self.metaparser_on_device_alias_context(device=device,
                                                       alias=None,
                                                       parser=parser)

    @keyword('parse "${parser:[^"]+}" on device "${device:[^"]+}" with '
             'context "${context}"')
    def metaparser_on_device_context(self, parser, device, context):
        '''Call any `metaparser` parser and parse the device output with
        a context (cli, xml, yang, ...)
        '''
        # Using `cli`, as its the default for RASTA
        return self.metaparser_on_device_alias_context(alias=None,
                                                       device=device,
                                                       parser=parser,
                                                       context=context)

    @keyword('parse "${parser:[^"]+}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}"')
    def metaparser_on_device_alias(self, parser, device, alias):
        '''Call any `metaparser` parser and parse the device using a specific
        alias
        '''
        return self.metaparser_on_device_alias_context(alias=alias,
                                                       device=device,
                                                       parser=parser)

    @keyword('parse "${parser:[^"]+}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}" with context "${context}"')
    def metaparser_on_device_alias_context(self, parser, device, alias,
                                           context='cli'):
        '''Call any `metaparser` parser and parse the device using a specific
        alias with a context (cli, xml, yang, ...)
        '''
        device_handle = self._search_device(device)

        # Look for the alias. If it doesnt exist,  let it crash to the user as
        # only valid alias should be provided
        con = device_handle
        if alias:
            con = getattr(device_handle, alias)

        # We dont know which of the python path is the package,
        # or even if there is an abstract package. It would be easy to fix, but
        # would need to ask for package name in Robot. It is decided to not
        # ask, but only support parser.<...> for abstraction, and everything
        # else would be just straight import 
        package, attr_name = parser.split('.', 1)

        # Make sure package is parser
        if package != 'parser':
            # then no abstraction, just load it
            package = None
            attr_name = parser

        # Find the right library with abstraction if package is not None
        try:
            cls = load_attribute(package, attr_name, device=device_handle)
        except Exception as e:
            if package:
                msg = "Could not find {p}.'{a}'".format(p=package, a=attr_name)
            else:
                msg = "Could not find '{a}'".format(a=attr_name)
            raise Exception(msg) from e

        # Instantiate the parser with the connection implementation
        parser = cls(con)
        return getattr(parser, context)()

    # Genie Ops
    @keyword('learn "${feature:[^"]+}" on device "${device:[^"]+}"')
    def genie_ops_on_device(self, feature, device):
        '''Learn Ops feature on device'''
        return self.genie_ops_on_device_alias_context(feature=feature,
                                                      alias=None,
                                                      device=device)

    @keyword('learn "${feature:[^"]+}" on device "${device:[^"]+}" with '
             'context "${context:[^"]+}"')
    def genie_ops_on_device_context(self, feature, device, context):
        '''Learn Ops feature on device with a context (cli, xml, yang, ...)'''
        return self.genie_ops_on_device_alias_context(feature=feature,
                                                        alias=None,
                                                        context=context,
                                                        device=device)

    @keyword('learn "${feature:[^"]+}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}"')
    def genie_ops_on_device_alias(self, feature, device, alias):
        '''Learn Ops feature on device using a specific alias'''
        return self.genie_ops_on_device_alias_context(feature=feature,
                                                        alias=alias,
                                                        device=device)

    @keyword('learn "${feature:[^"]+}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}" with context "${context:[^"]+}"')
    def genie_ops_on_device_alias_context(self, feature, device, alias,
                                          context='cli'):
        '''Learn Ops feature on device using a specific alias with a context
        (cli, xml, yang, ...)
        '''
        device_handle = self._search_device(device)

        # Look for the alias. If it doesnt exist,  let it crash to the user as
        # only valid alias should be provided
        con = device_handle
        if alias:
            con = getattr(device_handle, alias)
            device_handle.mapping[alias] = con

        # Find the feature for this device
        # 1) Directory must exists in genie.libs.ops.<feature>
        # 2) Then abstraction will kick in to find the right one.
        # 3) The directory syntax is <feature>.<feature.<Feature> 
        #    Where the class is capitalized but the directory/files arent.

        # First import genie.libs for abstraction
        package = 'genie.libs'

        try:
            mod = importlib.import_module(package)
        except ImportError as e:
            raise ImportError("package 'genie' and library 'genie.libs' "
                              "are mandatory to have to learn '{f}' "
                              .format(f=feature)) from e

        # Now find the right library
        attr_name = '.'.join(['ops', feature.lower(), feature.lower(),
                              feature.title()])

        # Find the right library with abstraction if needed
        # Get context in there
        added_context = False
        if hasattr(device_handle, 'custom') and\
           'abstraction' in device_handle.custom and\
           'order' in device_handle.custom['abstraction']:
               # Add context to it
               backup_abstraction = deepcopy(device_handle.custom['abstraction'])
               device_handle.custom['abstraction']['order'].append('context')
               device_handle.custom['abstraction']['context'] = context
               added_context = True

        try:
            cls = load_attribute(package, attr_name, device=device_handle)
        except Exception as e:
            msg = "Could not find {p}.'{a}'".format(p=package, a=attr_name)
            raise Exception(msg) from e

        if added_context:
            device_handle.custom['abstraction'] = backup_abstraction

        # Call the Ops now
        ops = cls(device_handle)
        ops.learn()
        return ops

    @keyword('Run verification "${name:[^"]+}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}"')
    def genie_run_verification_alias(self, name, device, alias):
        '''Call any verification defined in the verification datafile
           on device using a specific alias
        '''
        return self.genie_run_verification_alias_context(name=name,
                                                             alias=alias,
                                                             device=device,
                                                             context='cli')

    @keyword('Run verification "${name:[^"]+}" on device "${device:[^"]+}" '
             'with context "${context:[^"]+}"')
    def genie_run_verification_context(self, name, device, context):
        '''Call any verification defined in the verification datafile
           on device with a context (cli, xml, yang, ...)
        '''
        return self.genie_run_verification_alias_context(name=name,
                                                               alias=None,
                                                               device=device,
                                                               context=context)

    @keyword('Run verification "${name:[^"]+}" on device "${device:[^"]+}"')
    def genie_run_verification(self, name, device):
        '''Call any verification defined in the verification datafile
           on device
        '''
        return self.genie_run_verification_alias_context(name=name,
                                                               alias=None,
                                                               device=device,
                                                               context='cli')

    @keyword('Run verification "${name:[^"]+}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}" with context "${context:[^"]+}"')
    def genie_run_verification_alias_context(self, name, device, alias,
                                                 context):
        '''Call any verification defined in the verification datafile
           on device using a specific alias with a context (cli, xml, yang, ...)
        '''
        # Set the variables to find the verification
        self.testscript.verification_uids = Or(name)
        self.testscript.verification_groups = None
        self.testscript.verifications = deepcopy(self.verification_datafile)
        self.testscript.triggers = None

        # Modify the parameters to include context
        if name in self.testscript.verifications:
            # Add new parameters named context
            # No need to revert, as a deepcopy was taken, and after discovery
            # nothing is done with the datafiles after
            if 'devices' in self.testscript.verifications[name]:
                # For each device add context
                for dev in self.testscript.verifications[name]['devices']:
                    if self.testscript.verifications[name]['devices'][dev] == \
                       'None':
                        self.testscript.verifications[name]['devices'][dev] = {}
                    self.testscript.verifications[name]['devices'][dev]\
                                                 ['context'] = context

        self._run_genie_trigger_verification(name=name, alias=alias,
                                                 device=device, context=context)

    @keyword('Run trigger "${name:[^"]+}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}"')
    def genie_run_trigger_alias(self, name, device, alias):
        '''Call any trigger defined in the trigger datafile on device
        using a specific alias
        '''
        return self.genie_run_trigger_alias_context(name=name,
                                                          alias=alias,
                                                          device=device,
                                                          context='cli')

    @keyword('Run trigger "${name:[^"]+}" on device "${device:[^"]+}" '
             'with context "${context:[^"]+}"')
    def genie_run_trigger_context(self, name, device, context):
        '''Call any trigger defined in the trigger datafile on device
        with a context (cli, xml, yang, ...)
        '''
        return self.genie_run_trigger_alias_context(name=name,
                                                          alias=None,
                                                          device=device,
                                                          context=context)

    @keyword('Run trigger "${name:[^"]+}" on device "${device:[^"]+}"')
    def genie_run_trigger(self, name, device):
        '''Call any trigger defined in the trigger datafile on device
        '''
        return self.genie_run_trigger_alias_context(name=name,
                                                        alias=None,
                                                        device=device,
                                                        context='cli')

    @keyword('Run trigger "${name}" on device "${device:[^"]+}" '
             'using alias "${alias:[^"]+}" with context "${context:[^"]+}"')
    def genie_run_trigger_alias_context(self, name, device, alias, context):
        '''Call any trigger defined in the trigger datafile on device
        using a specific alias with a context (cli, xml, yang, ...)
        '''

        # Set the variables to find the trigger
        #import pdb,sys; pdb.Pdb(stdout=sys.__stdout__).set_trace()
        device_handle = self._search_device(device)

        self.testscript.trigger_uids = Or(name)
        self.testscript.trigger_groups = None
        self.testscript.triggers = deepcopy(self.trigger_datafile)
        self.testscript.verifications = None

        # Modify the parameters to include context
        self._add_abstraction_datafiles(datafile=self.testscript.triggers,
                                        name=name,
                                        context=context,
                                        device=device_handle)


        self._run_genie_trigger_verification(name=name, alias=alias,
                                                 device=device, context=context)

    @keyword('verify count "${number:[^"]+}" "${structure:[^"]+}" on device "${device:[^"]+}"')
    def verify_count(self, number, structure, device):
        '''Verify that a specific number of <...> is <...> on a device.

           Supports the same functionality as the alias keyword.
        '''
        return self.verify_count_alias(number, structure, device)

    @keyword('verify count "${number:[^"]+}" "${structure:[^"]+}" '
             'on device "${device:[^"]+}" using alias "${alias:[^"]+}"')
    def verify_count_alias(self, number, structure, device, alias=None):
        '''Verify that a specific number of <...> is <...> on a device using a
        specific alias

           verify count "<number>" "bgp neighbors" on device "<device>"

           verify count "<number>" "bgp routes" on device "<device>"

           verify count "<number>" "ospf neighbors" on device "<device>"

           verify count "<number>" "interfaces neighbors" on device "<device>"
        '''
        # First word of action is the protocol
        # Last word is the expected value
        # the rest is the structure.
        protocol, structure = structure.split(' ', 1)

        # Make sure we support this protocol
        count = 0
        if protocol == 'bgp':
            # Load bgp
            if structure == 'neighbors':
                # then count the number of neighbor
                ops = self.genie_ops_on_device_alias('bgp', device, alias)
                rs = [R(['info', 'instance', '(?P<instance>.*)', 'vrf', '(?P<vrf>.*)', 'neighbor', '(?P<neighbor>.*)', 'session_state', 'established'])]

            elif structure == 'routes':
                # then count the number of routes
                ops = self.genie_ops_on_device_alias('bgp', device, alias)
                rs = [R(['table', 'instance', '(?P<instance>.*)', 'vrf', '(?P<vrf>.*)', 'address_family', '(?P<af>.*)', 'prefixes', '(?P<routes>.*)', '(?P<rest>.*)'])]

        elif protocol == 'ospf':
            # Load ospf
            if structure == 'neighbors':
                # then count the number of neighbor
                ops = self.genie_ops_on_device_alias('ospf', device, alias)
                rs = [R(['info', 'vrf', '(?P<vrf>.*)', 'address_family', '(?P<af>.*)', 'instance', '(?P<instance>.*)', 'areas', '(?P<areas>.*)', '(?P<mode>.*)', '(?P<interface>.*)', 'neighbors', '(?P<neighbors>.*)', 'state', 'full'])]

        elif protocol == 'interface':
            if structure == 'up':
                # then count the number of interface
                ops = self.genie_ops_on_device_alias('interface', device, alias)
                rs = [R(['info', '(?P<interface>.*)', 'oper_status', 'up'])]

        count = len(find([ops], *rs, filter_=False, all_keys=True))
        if count != int(number):
            self.builtin.fail("Expected {e}, but found '{f}'".format(e=number,
                                                                     f=count ))

    def _run_genie_trigger_verification(self, alias, device, context,
                                            name):
        try:
            device_handle = self._search_device(device)
        except Exception as e:
            raise Exception("Could not find '{d}'".format(d=device))

        genie_discovery = GenieScriptDiscover(self.testscript)

        # To call the __iter__ of the discovery which will force
        # The generator to return all the elements
        sections = list(genie_discovery)

        # Remove both common sections
        testcases = sections[1:-1]

        # Its possible multiple devices were found, only
        # keep the one with the correct device
        tc_to_run = []
        for tc in testcases:
            # Make sure the device match the right device and
            # Make sure it match the name, as
            # Or logic could match more than expected
            if tc.parameters['uut'] != device_handle or\
               not re.match(name+'\.', tc.uid):
                continue
            tc_to_run.append(tc)

        # Make sure only len of 1
        if len(tc_to_run) == 0:
            raise Exception("Could not find '{r}'".format(r=name))

        if len(tc_to_run) != 1:
            raise Exception("Requested to run '{r}' but more than one was "
                            "found '{v}'".format(r=name,
                                                 v=', '.join(tc_to_run)))

        # Get the testcase class
        cls = tc_to_run[0]
        # Add to Cls the context if any

        # Set the tags
        tags = cls.groups if hasattr(cls, 'groups') else []

        # Found our testcase - Now Execute it
        try:
            # Make sure its reset, as we dont need some of these functionalities
            executer.reset()
            reporter.reset()
            result = cls()
        except Exception as e:
            # No need, as pyats has already logged the error
            pass

        # Maps the result RobotFramework
        self._convert_result(result, name, ' '.join(tags))

    def _add_abstraction_datafiles(self, datafile, name, device, context):
        '''Add context abstraction'''

        if name not in datafile or 'devices' not in datafile[name]:
            return datafile

        if device.name in datafile[name]['devices']:
            dev = device.name
        elif device.alias in datafile[name]['devices']:
            dev = device.alias
        else:
            return datafile

        # Nothing under device
        # Or device does not have abstraction
        if datafile[name]['devices'][dev] == 'None' or\
           'abstraction' not in datafile[name]['devices'][dev]:
            # Then add it at the trigger/verification level
            self._add_abstraction_at_level(datafile=datafile[name],
                                           context=context)
        else:
            # This there is information at device level and abstraction is there
            # Then add at device level
            self._add_abstraction_at_level(\
                    datafile=datafile[name]['devices'][dev],
                    context=context)
        return datafile

    def _add_abstraction_at_level(self, datafile, context):
        #    If abstraction does not exists, just add it
        #    If it already exists, then overwrite the information
        if 'abstraction' not in datafile:
            datafile['abstraction'] = {}

        datafile['abstraction']['context'] = context
        if 'order' not in datafile['abstraction']:
            datafile['abstraction']['order'] = []
        if 'context' not in datafile['abstraction']['order']:
            datafile['abstraction']['order'].append('context')

    def _convert_result(self, result, name, tags):
        ''''
            pyATS    RobotFramework  Reason
            Passed   Pass            Passed is a pass
            Failed   Fail            Failed is a fail
            Aborted  Fail            An abort is because of a failure
            Errored  Fail            An error is because of a failure
            Skipped  Pass            A skip is not a failure
            Blocked  Pass            A block is not a failure
            Passx    Pass            Passx is a pass with exception
        '''
        fail_group = [Failed, Aborted, Errored]
        pass_group = [Passed, Skipped, Blocked, Passx]

        if result in fail_group:
            self.builtin.fail('{n} has {r}'.format(n=name, r=result.name),
                              tags)

        if result in pass_group:
            self.builtin.pass_execution('{n} has {r}'.format(n=name,
                                                             r=result.name),
                                        tags)

        raise Exception('{r} is not a supported result'.format(r=result.name))

    def _search_device(self, name):
        try:
            # Find hostname and alias
            return self.testbed.devices[name]
        except KeyError:
            raise KeyError("Unknown device {}".format(name))
        except AttributeError as e:
            raise AttributeError("It is mandatory to 'use genie testbed ' "
                                 "command first.") from e

    def _load_genie_datafile(self):
        # Load the datafiles
        variables = self.builtin.get_variables()
        datafiles = []

        Datafile = namedtuple('Datafile', ['cls', 'name', 'padding'])
        datafiles.append(Datafile(cls=VerificationdatafileLoader,
                                  name='verification_datafile',
                                  padding='${verification_datafile}'))

        datafiles.append(Datafile(cls=TriggerdatafileLoader,
                                  name='trigger_datafile',
                                  padding='${trigger_datafile}'))

        for variable in datafiles:
            if variable.padding not in variables:
                continue

            datafile = variables[variable.padding]
            try:
                loader = variable.cls()
                # Remove the extra padding for variable
                setattr(self, variable.name, loader.load(datafile))
            except Exception as e:
                raise DatafileError("Failed to load the datafile '%s'"
                                    % datafile) from e


def load_attribute(pkg, attr_name, device=None):
    # a pkg is used for abstraction, as the package has to be imported
    # first
    if pkg:
        # Get package name
        pkg_name = pkg.split('.')[-1]
        # import it; if it explodes,  then it will error
        attribute = importlib.import_module(name=pkg)
        if getattr(attribute, '__abstract_pkg', None):
            # Lookup is cached,  so only the first time will be slow
            # Otherwise it is fast
            attribute = Lookup.from_device(device,
                                           packages={pkg_name:attribute})
            # Build the class to use to call
            items = attr_name.split('.')
            items.insert(0, pkg_name)
        else:
            items = attr_name.split('.')

        for item in items:
            try:
                attribute = getattr(attribute, item)
            except Exception as e:
                pass
    else:
        # So just load it up
        module, attr_name = attr_name.rsplit('.', 1)

        # Load the module
        mod = importlib.import_module(module)

        # Find the right attribute
        try:
            attribute = getattr(mod, attr_name)
        except AttributeError as e:
            raise AttributeError("Couldn't find '{name}' in "
                                 "'{mod}'".format(name=attr_name,
                                                  mod=mod)) from e
    return attribute


class Testscript(object):
    pass