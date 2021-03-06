#!/usr/bin/env python2
#
# Copyright (C) 2014 eNovance SAS <licensing@enovance.com>
#
# Author: Erwan Velu <erwan.velu@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from SocketServer import BaseRequestHandler, ThreadingTCPServer
import ConfigParser
import socket
import struct
from health_messages import Health_Message as HM
import health_libs as HL
import health_protocol as HP
import logging
import os
import pprint
import sys
import threading
import time
import yaml

socket_list = {}
lock_socket_list = threading.RLock()
hosts = {}
lock_host = threading.RLock()
hosts_state = {}
results_cpu = {}
serv = 0
NOTHING_RUN = 0
CPU_RUN = 1 << 0
MEMORY_RUN = 1 << 1
STORAGE_RUN = 1 << 2
NETWORK_RUN = 1 << 3


class SocketHandler(BaseRequestHandler):
    global hosts
    global lock_host
    timeout = 5
    disable_nagle_algorithm = False  # Set TCP_NODELAY socket option

    def handle(self):
        lock_socket_list.acquire()
        socket_list[self.client_address] = self.request
        lock_socket_list.release()

        HP.logger.debug('Got connection from %s' % self.client_address[0])
        while True:
            msg = HP.recv_hm_message(socket_list[self.client_address])
            if not msg:
                continue
            if msg.message != HM.ACK:
                if msg.message == HM.DISCONNECT:
                    HP.logger.debug('Disconnecting from %s' %
                                    self.client_address[0])

                    lock_host.acquire()
                    del hosts[self.client_address]
                    del hosts_state[self.client_address]
                    lock_host.release()

                    socket_list[self.client_address].close()

                    lock_socket_list.acquire()
                    del socket_list[self.client_address]
                    lock_socket_list.release()
                    return
                else:
                    lock_host.acquire()
                    hosts[self.client_address] = msg
                    hosts_state[self.client_address] = NOTHING_RUN
                    lock_host.release()

                    if msg.message == HM.MODULE and msg.action == HM.COMPLETED:
                        if msg.module == HM.CPU:
                            cpu_completed(self.client_address, msg)


def createAndStartServer():
    global serv
    ThreadingTCPServer.allow_reuse_address = True
    serv = ThreadingTCPServer(('', 20000), SocketHandler,
                              bind_and_activate=False)
    l_onoff = 1
    l_linger = 0
    serv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                           struct.pack('ii', l_onoff, l_linger))
    serv.server_bind()
    serv.server_activate()
    HP.logger.info('Starting server')
    serv.serve_forever()        # blocking method


def cpu_completed(host, msg):
    global hosts_state
    global results_cpu
    hosts_state[host] &= ~CPU_RUN
    results_cpu[host] = msg.hw


def get_host_list(item):
    global hosts
    global hosts_state
    selected_hosts = {}
    for host in hosts.keys():
        if hosts_state[host] & item == item:
            selected_hosts[host] = True

    return selected_hosts


def start_cpu_bench(nb_hosts, runtime, cores):
    global hosts_state
    msg = HM(HM.MODULE, HM.CPU, HM.START)
    msg.cpu_instances = cores
    msg.running_time = runtime
    for host in hosts.keys():
        if nb_hosts == 0:
            break
        if not host in get_host_list(CPU_RUN).keys():
            hosts_state[host] |= CPU_RUN
            nb_hosts = nb_hosts - 1
            lock_socket_list.acquire()
            HP.send_hm_message(socket_list[host], msg)
            lock_socket_list.release()


def disconnect_clients():
    global serv
    msg = HM(HM.DISCONNECT)
    HP.logger.info("Asking %d hosts to disconnect" % len(hosts.keys()))
    for host in hosts.keys():
            lock_socket_list.acquire()
            HP.send_hm_message(socket_list[host], msg)
            lock_socket_list.release()

    while(hosts.keys()):
        time.sleep(1)
        HP.logger.info("Still %d hosts connected" % len(hosts.keys()))

    HP.logger.info("All hosts disconnected")
    serv.shutdown()
    serv.socket.close()


def save_hw(items, name, hwdir):
    'Save hw items for inspection on the server.'
    try:
        filename = os.path.join(hwdir, name + '.hw')
        pprint.pprint(items, stream=open(filename, 'w'))
    except Exception, xcpt:
        HP.logger.error("exception while saving hw file: %s" % str(xcpt))


def compute_results(nb_hosts):
    config = ConfigParser.ConfigParser()
    config.read('/etc/edeploy.conf')

    def config_get(section, name, default):
        'Secured config getter.'
        try:
            return config.get(section, name)
        except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
            return default

    cfg_dir = os.path.normpath(config_get('SERVER', 'HEALTHDIR', '')) + '/'
    dirname = time.strftime("%Y_%m_%d-%Hh%M", time.localtime())
    dest_dir = cfg_dir + 'dahc/%d/' % nb_hosts + dirname

    try:
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir)
    except OSError, e:
        fatal_error("Cannot create %s directory (%s)" % (dest_dir, e.errno))

    for host in results_cpu.keys():
        HP.logger.info("Dumping cpu result from host %s" % str(host))
        filename_and_macs = HL.generate_filename_and_macs(results_cpu[host])
        save_hw(results_cpu[host], filename_and_macs['sysname'], dest_dir)
#        print results_cpu[host]


def get_default_value(job, item, default_value):
    return job.get(item, default_value)


def non_interactive_mode(filename):
    total_runtime = 0
    name = "undefined"

    job = yaml.load(file(filename, 'r'))
    if job['name'] is None:
        HP.logger.error("Missing name parameter in yaml file")
        disconnect_clients()
        return
    else:
        name = job['name']

    if job['required-hosts'] is None:
        HP.logger.error("Missing required-hosts parameter in yaml file")
        disconnect_clients()
        return

    required_hosts = job['required-hosts']
    if required_hosts < 1:
        HP.logger.error("required-hosts shall be greater than 0")
        disconnect_clients()
        return

    runtime = get_default_value(job, 'runtime', 0)

    HP.logger.info("Expecting %d hosts to start job %s" %
                   (required_hosts, name))
    hosts_count = len(hosts.keys())
    previous_hosts_count = hosts_count
    while (int(hosts_count) < int(required_hosts)):
	if (hosts_count != previous_hosts_count) :
		HP.logger.info("Still %d hosts to connect" % (int(required_hosts) - int(hosts_count)))
		previous_hosts_count = hosts_count
	hosts_count = len(hosts.keys())
        time.sleep(1)

    HP.logger.info("Starting job %s" % name)
    cpu_job = job['cpu']
    if cpu_job:
            step_hosts = get_default_value(cpu_job, 'step-hosts', 1)
            required_cpu_hosts = get_default_value(cpu_job, 'required-hosts',
                                                   required_hosts)
            if "-" in str(required_cpu_hosts):
                min_hosts = int(str(required_cpu_hosts).split("-")[0])
                max_hosts = int(str(required_cpu_hosts).split("-")[1])
            else:
                min_hosts = required_cpu_hosts
                max_hosts = min_hosts

            if max_hosts < 1:
                max_hosts = min_hosts
                HP.logger.error("CPU: required-hosts shall be greater than"
                                " 0, defaulting to global required-hosts=%d"
                                % max_hosts)

            if max_hosts > required_hosts:
                HP.logger.error("CPU: The maximum number of hosts to tests"
                                " is greater than the amount of available"
                                " hosts.")
                HP.logger.error("CPU: Canceling Test")
            else:
                for nb_hosts in xrange(min_hosts, max_hosts+1, step_hosts):
                    cpu_runtime = get_default_value(cpu_job, 'runtime',
                                                    runtime)
                    HP.logger.info("CPU: Waiting bench %d / %d (step = %d)"
                                   " to finish on %d hosts : should take"
                                   " %d seconds" % (nb_hosts, max_hosts,
                                                    step_hosts, nb_hosts,
                                                    cpu_runtime))
                    total_runtime += cpu_runtime
                    cores = get_default_value(cpu_job, 'cores', 1)
                    start_cpu_bench(nb_hosts, cpu_runtime, cores)

                    time.sleep(cpu_runtime)

                    while (get_host_list(CPU_RUN).keys()):
                        time.sleep(1)

                    compute_results(nb_hosts)

    HP.logger.info("End of job %s" % name)
    disconnect_clients()


if __name__ == '__main__':

    HP.start_log('/var/tmp/health-server.log', logging.DEBUG)

    if len(sys.argv) < 2:
        HP.logger.error("You must provide a yaml file as argument")
        sys.exit(1)

    myThread = threading.Thread(target=createAndStartServer)
    myThread.start()

    non_interactive = threading.Thread(target=non_interactive_mode,
                                       args=tuple([sys.argv[1]]))
    non_interactive.start()
