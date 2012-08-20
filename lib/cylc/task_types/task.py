#!/usr/bin/env python

#C: THIS FILE IS PART OF THE CYLC FORECAST SUITE METASCHEDULER.
#C: Copyright (C) 2008-2012 Hilary Oliver, NIWA
#C:
#C: This program is free software: you can redistribute it and/or modify
#C: it under the terms of the GNU General Public License as published by
#C: the Free Software Foundation, either version 3 of the License, or
#C: (at your option) any later version.
#C:
#C: This program is distributed in the hope that it will be useful,
#C: but WITHOUT ANY WARRANTY; without even the implied warranty of
#C: MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#C: GNU General Public License for more details.
#C:
#C: You should have received a copy of the GNU General Public License
#C: along with this program.  If not, see <http://www.gnu.org/licenses/>.

# This module uses the @classmethod decorator, introduced in Python 2.4.
# . @classmethod
# . def foo( bar ):
# .   pass
# Equivalent Python<2.4 form:
# . def foo( bar ):
# .   pass
# . foo = classmethod( foo )

# TASK PROXY BASE CLASS:

import sys, re
import datetime
from cylc import task_state
from cylc.RunEventHandler import RunHandler
import logging
import Pyro.core

def displaytd( td ):
    # Display a python timedelta sensibly.
    # Default for str(td) of -5 sec is '-1 day, 23:59:55' !
    d, s, m = td.days, td.seconds, td.microseconds
    secs = d * 24 * 3600 + s + m / 10**6
    if secs < 0:
        res = '-' + str( datetime.timedelta( 0, - secs, 0 ))
    else:
        res = str(td)
    return res

# NOTE ON TASK STATE INFORMATION---------------------------------------

# task attributes required for a system cold start are:
#  state ('waiting', 'submitted', 'running', and 'succeeded' or 'failed')

# The 'state' variable is initialised by the base class, and written to
# the state dump file by the base class dump_state() method.

# For a restart from previous state some tasks may require additional
# state information to be stored in the state dump file.

# To handle this difference in initial state information (between normal
# start and restart) task initialisation must use a default value of
# 'None' for the additional variables, and for a restart the task
# manager must instantiate each task with a flattened list of all the
# state values found in the state dump file.

# NOTE ON EXECUTION OF EVENT HOOK SCRIPTS:
# These have to be executed in the background because (a) they could
# take a long time to execute, or (b) they could try to operate on the
# suite in some way (e.g. to remove a failed task automatically) - this
# would create a deadlock if cylc waited on them to complete before
# carrying on. Consequently cylc cannot to detect failure of a hook
# script (not easily at least ... could check on the process in a
# background thread...?)

class task( Pyro.core.ObjBase ):

    clock = None
    intercycle = False
    suite = None
    state_changed = True

    @classmethod
    def describe( cls ):
        return cls.description

    @classmethod
    def set_class_var( cls, item, value ):
        # set the value of a class variable
        # that will be written to the state dump file
        try:
            cls.class_vars[ item ] = value
        except AttributeError:
            cls.class_vars = {}
            cls.class_vars[ item ] = value

    @classmethod
    def get_class_var( cls, item ):
        # get the value of a class variable that is
        # written to the state dump file
        try:
            return cls.class_vars[ item ]
        except:
            raise AttributeError

    @classmethod
    def dump_class_vars( cls, FILE ):
        # dump special class variables to the state dump file
        try:
            result = ''
            for key in cls.class_vars:
                result += key + '=' + str( cls.class_vars[ key ] ) + ', '
            result = result.rstrip( ', ' )
            FILE.write( 'class ' + cls.__name__ + ' : ' + result + '\n')
        except AttributeError:
            # class has no class_vars defined
            pass

    @classmethod
    def update_mean_total_elapsed_time( cls, started, succeeded ):
        # the class variables here are defined in derived task classes
        cls.elapsed_times.append( succeeded - started )
        elt_sec = [x.days * 86400 + x.seconds for x in cls.elapsed_times ]
        mtet_sec = sum( elt_sec ) / len( elt_sec )
        cls.mean_total_elapsed_time = datetime.timedelta( seconds=mtet_sec )

    def __init__( self, state ):
        # Call this AFTER derived class initialisation

        # Derived class init MUST define:
        #  * self.id: unique identity (e.g. NAME%CYCLE for cycling tasks)
        #  * prerequisites and outputs
        #  * self.env_vars

        class_vars = {}
        self.state = task_state.task_state( state )
        self.launcher = None
        self.trigger_now = False

        # Count instances of each top level object derived from task.
        # Top level derived classes must define:
        #   <class>.instance_count = 0
        # NOTE: top level derived classes are now defined dynamically
        # (so this is initialised in src/taskdef.py).
        self.__class__.instance_count += 1
        self.__class__.upward_instance_count += 1

        Pyro.core.ObjBase.__init__(self)

        self.latest_message = ""
        self.latest_message_priority = "NORMAL"

        self.submission_timer_start = None
        self.execution_timer_start = None

        self.submitted_time = None
        self.started_time = None
        self.succeeded_time = None
        self.etc = None
        self.to_go = None
        self.try_number = 1
        self.retry_delay_timer_start = None

    def plog( self, message ):
        # print and log a low priority message
        print self.id + ':', message
        self.log( 'NORMAL', message )

    def log( self, priority, message ):
        logger = logging.getLogger( "main" )
        message = '[' + self.id + '] -' + message
        if priority == "WARNING":
            logger.warning( message )
        elif priority == "NORMAL":
            logger.info( message )
        elif priority == "DEBUG":
            logger.debug( message )
        elif priority == "CRITICAL":
            logger.critical( message )
        else:
            logger.warning( 'UNKNOWN PRIORITY: ' + priority )
            logger.warning( '-> ' + message )

    def prepare_for_death( self ):
        # The task manager MUST call this immediately before deleting a
        # task object. It decrements the instance count of top level
        # objects derived from task base. It would be nice to use Python's
        # __del__() function for this, but that is only called when a
        # deleted object is about to be garbage collected (which is not
        # guaranteed to be right away). This was once used for
        # constraining the number of instances of each task type.
        self.__class__.instance_count -= 1

    def ready_to_run( self ):
        ready = False
        if self.state.is_queued() or \
            self.state.is_waiting() and self.prerequisites.all_satisfied():
                if self.retry_delay_timer_start:
                     diff = task.clock.get_datetime() - self.retry_delay_timer_start
                     foo = datetime.timedelta( 0,0,0,0,self.retry_delay,0,0 )
                     if diff >= foo:
                        ready = True
                else:
                        ready = True
        return ready

    def get_resolved_dependencies( self ):
        dep = []
        satby = self.prerequisites.get_satisfied_by()
        for label in satby.keys():
            dep.append( satby[ label ] )
        return dep

    def call_warning_hook( self, message ):
        self.plog( 'calling task warning hook script' )
        RunHandler( 'warning', self.__class__.hook_script, self.__class__.suite, self.id, message )

    def set_submitted( self ):
        self.state.set_status( 'submitted' )
        self.log( 'NORMAL', "job submitted" )
        self.submitted_time = task.clock.get_datetime()
        self.submission_timer_start = self.submitted_time
        if 'submitted' in self.__class__.hook_events and self.__class__.hook_script:
            self.plog( 'calling task submitted hook script' )
            RunHandler( 'submitted', self.__class__.hook_script, self.__class__.suite, self.id, '(task submitted)' )

    def set_running( self ):
        self.state.set_status( 'running' )
        self.started_time = task.clock.get_datetime()
        self.started_time_real = datetime.datetime.now()
        self.execution_timer_start = self.started_time
        if 'started' in self.__class__.hook_events and self.__class__.hook_script:
            self.plog( 'calling task started hook script' )
            RunHandler( 'started', self.__class__.hook_script, self.__class__.suite, self.id, '(task started)' )

    def set_succeeded( self ):
        self.outputs.set_all_completed()
        self.state.set_status( 'succeeded' )
        self.succeeded_time = task.clock.get_datetime()
        # don't update mean total elapsed time if set_succeeded() was called

    def set_succeeded_hook( self ):
        # (set_succeeded() is used by remote switch)
        print '\n' + self.id + " SUCCEEDED"
        self.state.set_status( 'succeeded' )
        if 'succeeded' in self.__class__.hook_events and self.__class__.hook_script:
            self.plog( 'calling task succeeded hook script' )
            RunHandler( 'succeeded', self.__class__.hook_script, self.__class__.suite, self.id, '(task succeeded)' )

    def set_failed( self, reason='(task failed)' ):
        self.state.set_status( 'failed' )
        self.log( 'CRITICAL', reason )
        if 'failed' in self.__class__.hook_events and self.__class__.hook_script:
            self.plog( 'calling task failed hook script' )
            RunHandler( 'failed', self.__class__.hook_script, self.__class__.suite, self.id, reason )

    def set_submit_failed( self, reason='(job submission failed)' ):
        self.state.set_status( 'failed' )
        self.log( 'CRITICAL', reason )
        if 'submission_failed' in self.__class__.hook_events and self.__class__.hook_script:
            self.plog( 'calling task submission failed hook script' )
            RunHandler( 'submission_failed', self.__class__.hook_script, self.__class__.suite, self.id, reason )

    def unfail( self ):
        # if a task is manually reset remove any previous failed message
        # or on later success it will be seen as an incomplete output.
        failed_msg = self.id + " failed"
        if self.outputs.exists(failed_msg):
            self.outputs.remove(failed_msg)

    def reset_state_ready( self ):
        self.state.set_status( 'waiting' )
        self.prerequisites.set_all_satisfied()
        self.unfail()
        self.outputs.set_all_incomplete()

    def reset_state_waiting( self ):
        # waiting and all prerequisites UNsatisified.
        self.state.set_status( 'waiting' )
        self.prerequisites.set_all_unsatisfied()
        self.unfail()
        self.outputs.set_all_incomplete()

    def reset_state_succeeded( self ):
        # all prerequisites satisified and all outputs complete
        self.state.set_status( 'succeeded' )
        self.prerequisites.set_all_satisfied()
        self.unfail()
        self.outputs.set_all_completed()

    def reset_state_failed( self ):
        # all prerequisites satisified and no outputs complete
        self.state.set_status( 'failed' )
        self.prerequisites.set_all_satisfied()
        self.outputs.set_all_incomplete()
        # set a new failed output just as if a failure message came in
        self.outputs.add( self.id + ' failed', completed=True )

    def reset_state_held( self ):
        itask.state.set_status( 'held' )

    def submit( self, dry_run=False ):
        self.log( 'DEBUG',  'submitting task job script' )
        # construct the job launcher here so that a new one is used if
        # the task is re-triggered by the suite operator - so it will
        # get new stdout/stderr logfiles and not overwrite the old ones.

        # dynamic instantiation - don't know job sub method till run time.
        module_name = self.job_submit_method
        class_name  = self.job_submit_method
        # NOTE: not using__import__() keyword arguments:
        #mod = __import__( module_name, fromlist=[class_name] )
        # as these were only introduced in Python 2.5.
        try:
            # try to import built-in job submission classes first
            mod = __import__( 'cylc.job_submission.' + module_name, globals(), locals(), [class_name] )
        except ImportError:
            try:
                # else try for user-defined job submission classes, in sys.path
                mod = __import__( module_name, globals(), locals(), [class_name] )
            except ImportError, x:
                print >> sys.stderr, x
                raise SystemExit( 'ERROR importing job submission method: ' + class_name )

        launcher_class = getattr( mod, class_name )

        # To Do: most of the following arguments could be class variables
        self.launcher = launcher_class(
                        self.id, self.initial_scripting,
                        self.precommand, self.command, self.try_number,
                        self.postcommand, self.env_vars,
                        self.namespace_hierarchy, self.directives,
                        self.manual_messaging, self.logfiles,
                        self.__class__.job_submit_log_directory,
                        self.__class__.job_submit_share_directory,
                        self.__class__.job_submit_work_directory,
                        self.__class__.owner,
                        self.__class__.remote_host,
                        self.__class__.remote_cylc_directory,
                        self.__class__.remote_suite_directory,
                        self.__class__.remote_shell_template,
                        self.__class__.remote_log_directory,
                        self.__class__.job_submit_command_template,
                        self.__class__.job_submission_shell,
                        self.ssh_messaging )

        try:
            p = self.launcher.submit( dry_run )
        except:
            self.set_submit_failed()
            return None
        else:
            self.set_submitted()
            self.submission_timer_start = task.clock.get_datetime()
            return p

    def check_submission_timeout( self ):
        if not self.__class__.hook_script:
            # no event handler specified.
            return
        if 'submission_timeout' not in self.__class__.hook_events:
            # not handling timeouts
            return
        if not self.submission_timeout:
            # no timeout values specified
            return
        if not self.state.is_submitted() and not self.state.is_running():
            # nothing to time out yet
            return
        current_time = task.clock.get_datetime()
        if self.submission_timer_start != None and not self.state.is_running():
            timeout = self.submission_timer_start + datetime.timedelta( minutes=self.submission_timeout )
            if current_time > timeout:
                msg = 'submitted ' + str( self.submission_timeout ) + ' minutes ago, but has not started'
                self.log( 'WARNING', msg )
                self.plog( 'Calling task submission timeout hook script.' )
                RunHandler( 'submission_timeout', self.__class__.hook_script, self.__class__.suite, self.id, msg )
                self.submission_timer_start = None

    def check_execution_timeout( self ):
        if not self.__class__.hook_script:
            # no event handler specified.
            return
        if 'execution_timeout' not in self.__class__.hook_events:
            # not handling timeouts
            return
        if not self.execution_timeout:
            # no timeout values specified
            return
        if not self.state.is_submitted() and not self.state.is_running():
            # nothing to time out yet
            return
        current_time = task.clock.get_datetime()
        if self.execution_timer_start != None and self.state.is_running():
            # check for job execution timeout
            timeout = self.execution_timer_start + datetime.timedelta( minutes=self.execution_timeout )
            if current_time > timeout:
                if self.reset_timer:
                    msg = 'last message ' + str( self.execution_timeout ) + ' minutes ago, but has not succeeded'
                else:
                    msg = 'started ' + str( self.execution_timeout ) + ' minutes ago, but has not succeeded'
                self.log( 'WARNING', msg )
                self.plog( 'Calling task execution timeout hook script.' )
                RunHandler( 'execution_timeout', self.__class__.hook_script, self.__class__.suite, self.id, msg )
                self.execution_timer_start = None

    def sim_time_check( self ):
        if not self.state.is_running():
            return
        timeout = self.started_time_real + \
                datetime.timedelta( seconds=self.sim_mode_run_length )
        if datetime.datetime.now() > timeout:
            self.incoming( 'NORMAL', self.id + ' succeeded' )
            task.state_changed = True

    def set_all_internal_outputs_completed( self ):
        if self.reject_if_failed( 'set_all_internal_outputs_completed' ):
            return
        self.log( 'DEBUG', 'setting all internal outputs completed' )
        for message in self.outputs.completed:
            if message != self.id + ' started' and \
                    message != self.id + ' succeeded' and \
                    message != self.id + ' completed':
                self.incoming( 'NORMAL', message )

    def is_complete( self ):  # not needed?
        if self.outputs.all_completed():
            return True
        else:
            return False

    def reject_if_failed( self, message ):
        if self.state.is_failed():
            if self.__class__.resurrectable:
                self.log( 'WARNING', 'message receive while failed: I am returning from the dead!' )
                return False
            else:
                self.log( 'WARNING', 'rejecting a message received while in the failed state:' )
                self.log( 'WARNING', '  ' + message )
            return True
        else:
            return False

    def incoming( self, priority, message ):
        if self.__class__.hook_script and priority == 'WARNING':
            self.call_warning_hook( message )

        if self.reject_if_failed( message ):
            return

        if self.reset_timer:
            self.execution_timer_start = task.clock.get_datetime()

        # receive all incoming pyro messages for this task
        self.latest_message = message
        self.latest_message_priority = priority

        # setting state_changed results in task processing loop
        # invocation. We should really only do this when the
        # incoming message results in a state change that matters to
        # scheduling ... but system monitor may need latest message, and
        # we don't yet have a separate state-summary-update invocation
        # flag.

        task.state_changed = True

        if message == self.id + ' started':
            self.set_running()

        if not self.state.is_running():
            # my external task should not be running!
            self.log( 'WARNING', "UNEXPECTED MESSAGE (task should not be running)" )
            self.log( 'WARNING', '-> ' + message )

        if message == self.id + ' failed':
            # process task failure messages
            self.succeeded_time = task.clock.get_datetime()
            try:
                # Is there a retry lined up for this task?
                self.retry_delay = float(self.retry_delays.popleft())
            except IndexError:
                # Nope, we are now failed as.
                # Add the failed method as a task output so that other
                # tasks can trigger off the failure event (failure
                # outputs are not added in advance as under normal
                # circumstances they will not be completed outputs).
                self.outputs.add( message )
                self.outputs.set_completed( message )
                # this also calls the task failure hook script:
                self.set_failed( message )
                task.state_changed = True
            else:
                # Yep, we can retry.
                self.plog( 'Setting retry delay: ' + str(self.retry_delay) +  ' minutes' )
                self.retry_delay_timer_start = task.clock.get_datetime()
                self.try_number += 1
                self.state.set_status( 'retry_delayed' )
                self.prerequisites.set_all_satisfied()
                self.outputs.set_all_incomplete()
                task.state_changed = True
                if 'retry' in self.__class__.hook_events and self.__class__.hook_script:
                    self.plog( 'calling task retry hook script' )
                    RunHandler( 'retry', self.__class__.hook_script, self.__class__.suite, self.id, '(task retrying)' )

        elif self.outputs.exists( message ):
            # registered output messages

            if not self.outputs.is_completed( message ):
                # message indicates completion of a registered output.
                self.log( priority,  message )
                self.outputs.set_completed( message )

                if message == self.id + ' succeeded':
                    # TASK HAS SUCCEEDED
                    self.succeeded_time = task.clock.get_datetime()
                    self.__class__.update_mean_total_elapsed_time( self.started_time, self.succeeded_time )
                    if not self.outputs.all_completed():
                        self.set_failed( 'succeeded before all outputs were completed' )
                    else:
                        self.set_succeeded_hook()
            else:
                # this output has already been satisfied
                self.log( 'WARNING', "UNEXPECTED OUTPUT (already completed):" )
                self.log( 'WARNING', "-> " + message )

        else:
            # log other (non-failed) unregistered messages with a '*' prefix
            message = '*' + message
            self.log( priority, message )

    def update( self, reqs ):
        for req in reqs.get_list():
            if req in self.prerequisites.get_list():
                # req is one of my prerequisites
                if reqs.is_satisfied(req):
                    self.prerequisites.set_satisfied( req )

    def dump_state( self, FILE ):
        # Write state information to the state dump file
        # This must be compatible with __init__() on reload
        FILE.write( self.id + ' : ' + self.state.dump() + '\n' )

    def spawn( self, state ):
        self.state.set_spawned()
        return self.__class__( self.next_tag(), state )

    def has_spawned( self ):
        # the one off task type modifier overrides this.
        return self.state.has_spawned()

    def ready_to_spawn( self ):
        # return True or False
        self.log( 'CRITICAL', 'ready_to_spawn(): OVERRIDE ME')
        sys.exit(1)

    def done( self ):
        # return True if task has succeeded and spawned
        if self.state.is_succeeded() and self.state.has_spawned():
            return True
        else:
            return False

    def check_requisites( self ):
        # overridden by asynchronous tasks
        pass

    def get_state_summary( self ):
        # derived classes can call this method and then
        # add more information to the summary if necessary.

        n_total = self.outputs.count()
        n_satisfied = self.outputs.count_completed()

        summary = {}
        summary[ 'name' ] = self.name
        summary[ 'label' ] = self.tag
        summary[ 'state' ] = self.state.get_status()
        summary[ 'n_total_outputs' ] = n_total
        summary[ 'n_completed_outputs' ] = n_satisfied
        summary[ 'spawned' ] = self.state.has_spawned()
        summary[ 'latest_message' ] = self.latest_message
        summary[ 'latest_message_priority' ] = self.latest_message_priority

        if self.submitted_time:
            #summary[ 'submitted_time' ] = self.submitted_time.strftime("%Y/%m/%d %H:%M:%S" )
            summary[ 'submitted_time' ] = self.submitted_time.strftime("%H:%M:%S" )
        else:
            summary[ 'submitted_time' ] = '*'

        if self.started_time:
            #summary[ 'started_time' ] =  self.started_time.strftime("%Y/%m/%d %H:%M:%S" )
            summary[ 'started_time' ] =  self.started_time.strftime("%H:%M:%S" )
        else:
            summary[ 'started_time' ] =  '*'

        if self.succeeded_time:
            #summary[ 'succeeded_time' ] =  self.succeeded_time.strftime("%Y/%m/%d %H:%M:%S" )
            summary[ 'succeeded_time' ] =  self.succeeded_time.strftime("%H:%M:%S" )
        else:
            summary[ 'succeeded_time' ] =  '*'

        # str(timedelta) => "1 day, 23:59:55.903937" (for example)
        # to strip off fraction of seconds:
        # timedelta = re.sub( '\.\d*$', '', timedelta )

        # TO DO: the following section could probably be streamlined a bit
        if self.__class__.mean_total_elapsed_time:
            met = self.__class__.mean_total_elapsed_time
            summary[ 'mean total elapsed time' ] =  re.sub( '\.\d*$', '', str(met) )
            if self.started_time:
                if not self.succeeded_time:
                    # started but not succeeded yet, compute ETC
                    current_time = task.clock.get_datetime()
                    run_time = current_time - self.started_time
                    self.to_go = met - run_time
                    self.etc = current_time + self.to_go
                    summary[ 'Tetc' ] = self.etc.strftime( "%H:%M:%S" ) + '(' + re.sub( '\.\d*$', '', displaytd(self.to_go) ) + ')'
                elif self.etc:
                    # the first time a task finishes self.etc is not defined
                    # task succeeded; leave final prediction
                    summary[ 'Tetc' ] = self.etc.strftime( "%H:%M:%S" ) + '(' + re.sub( '\.\d*$', '', displaytd(self.to_go) ) + ')'
                else:
                    summary[ 'Tetc' ] = '*'
            else:
                # not started yet
                summary[ 'Tetc' ] = '*'
        else:
            # first instance: no mean time computed yet
            summary[ 'mean total elapsed time' ] =  '*'
            summary[ 'Tetc' ] = '*'

        summary[ 'logfiles' ] = self.logfiles.get_paths()

        return summary

    def not_fully_satisfied( self ):
        if not self.prerequisites.all_satisfied():
            return True
        if not self.suicide_prerequisites.all_satisfied(): # TO DO: IS THIS CORRECT?
            return True
        return False

    def satisfy_me( self, outputs ):
        self.prerequisites.satisfy_me( outputs )
        # TO DO: DONT DO THIS IF HAVE NO SUICIDE PREREQUISITES (efficiency reasons):
        self.suicide_prerequisites.satisfy_me( outputs )

    def adjust_tag( self, tag ):
        # Override to modify initial tag if necessary.
        return tag

    def next_tag( self ):
        # Asynchronous tasks: increment the tag by one.
        # Cycling tasks override this to compute their next valid cycle time.
        return str( int( self.tag ) + 1 )

    def is_cycling( self ):
        return False

    def is_daemon( self ):
        return False

    def is_clock_triggered( self ):
        return False
