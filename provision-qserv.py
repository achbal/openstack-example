#!/usr/bin/env python

"""
Boot instances in cluster infrastructure, and use cloud config to create users
and install packages on virtual machines

Script performs these tasks:
- launch instances from image and manage ssh key
- create gateway vm
- check for available floating ip adress
- add it to gateway
- cloud config

@author  Oualid Achbal, ISIMA student , IN2P3

"""

# -------------------------------
#  Imports of standard modules --
# -------------------------------
import logging
import os
import sys
import time
import warnings

# ----------------------------
# Imports for other modules --
# ----------------------------
from novaclient import client
import novaclient.exceptions

# -----------------------
# Exported definitions --
# -----------------------
def get_nova_creds():
    """
    Extract the login information from the environment
    """
    d = {}
    d['version'] = 2.4
    d['username'] = os.environ['OS_USERNAME']
    d['api_key'] = os.environ['OS_PASSWORD']
    d['auth_url'] = os.environ['OS_AUTH_URL']
    d['project_id'] = os.environ['OS_TENANT_NAME']
    d['insecure'] = True
    logging.debug("Openstack user: {}".format(d['username']))
    return d

def nova_servers_create(instance_id):
    """
    Boot an instance from an image and check status
    """
    instance_name = "{0}-qserv-{1}".format(creds['username'], instance_id)
    logging.info("Launch an instance {}".format(instance_name))

    # cloud config
    cloud_config_tpl = '''
    
    #cloud-config
    groups:
    - docker: [qserv]

    users:
    - default
    - name: qserv
      gecos: Qserv daemon
      sudo: ALL=(ALL) NOPASSWD:ALL
      shell: /bin/bash
      lock-passwd: true
      ssh-authorized-keys:
      - {key} 
    
    packages:
    - docker
    - epel-release
    
    runcmd:
    # nss-mdns is provided by epel-release
    # so it can not be installed with 'packages' directive
    - [ "yum", "-y", "update" ]
    - [ "yum", "-y", "install", "nss-mdns" ]
    - [ "service", "avahi-daemon", "start" ]
    - [ "hostname", "{hostname}" ]
    - [ "usermod", "-aG", "docker", "qserv" ]
    - [ "service", "docker", "start" ]

    manage_etc_hosts: true
    package_upgrade: true
    package_reboot_if_required: true
    timezone: Europe/Paris
    '''

    fpubkey = open(os.path.expanduser('~/.ssh/id_rsa.pub'))
    userdata = cloud_config_tpl.format(key=fpubkey.read(),
                                     hostname=instance_name)
     
    #logging.debug("cloud-config.txt {}:".format(userdata))

    # Launch an instance from an image
    instance = nova.servers.create(name=instance_name, image=image,
            flavor=flavor, userdata=userdata, key_name=key)
    # Poll at 5 second intervals, until the status is no longer 'BUILD'
    status = instance.status
    while status == 'BUILD':
        time.sleep(5)
        instance.get()
        status = instance.status
    logging.info ("status: {}".format(status))
    logging.info ("Instance {} is active".format(instance_name))

    return instance

def manage_ssh_key():
    """
    Upload ssh public key
    """
    logging.info('Manage ssh keys')
    if nova.keypairs.findall(name=key):
        logging.debug('Remove previous ssh keys')
        nova.keypairs.delete(key=key)

    with open(os.path.expanduser('~/.ssh/id_rsa.pub')) as fpubkey:
        nova.keypairs.create(name=key, public_key=fpubkey.read())

def get_floating_ip():
    """
    Allocate floating ip to project
    """
    i=0
    floating_ips = nova.floating_ips.list()
    floating_ip = None
    floating_ip_pool = nova.floating_ip_pools.list()[0].name

    # Check for available public ip in project
    while i<len(floating_ips) and floating_ip is None:
        if floating_ips[i].instance_id is None:
            floating_ip=floating_ips[i]
            logging.debug('Available floating ip found {}'.format(floating_ip))
        i+=1

    # Check for available public ip in ext-net pool
    if floating_ip is None:
        try:
            logging.debug("Use floating ip pool: {}".format(floating_ip_pool))
            floating_ip = nova.floating_ips.create(floating_ip_pool)
        except novaclient.exceptions.Forbidden as e:
            logging.fatal("Unable to retrieve public IP: {0}".format(e))
            sys.exit(1)

    return floating_ip

def nova_servers_delete(vm_name):
    """
    Retrieve an instance by name and shut it down
    """
    server = nova.servers.find(name=vm_name)
    server.delete()

def print_ssh_config(instances, floating_ip):
    """
    Print ssh client configuration to file
    """
    # ssh config
    ssh_config_tpl = '''

    Host {host}
    HostName {fixed_ip}
    User qserv
    Port 22
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    PasswordAuthentication no
    ProxyCommand ssh -W %h:%p qserv@{floating_ip}
    IdentityFile ~/.ssh/id_rsa
    IdentitiesOnly yes
    LogLevel FATAL
    '''

    ssh_config_extract = ""
    for instance in instances:
        fixed_ip = instance.networks['petasky-net'][0]
        ssh_config_extract += ssh_config_tpl.format(host=instance.name,
                                                    fixed_ip=fixed_ip,
                                                    floating_ip=floating_ip.ip)

    logging.debug("SSH client config: ")

    f = open("ssh_config", "w")
    f.write(ssh_config_extract)
    f.close()


if __name__ == "__main__":
    try:
        logging.basicConfig(format='%(asctime)s %(levelname)-8s %(name)-15s %(message)s',level=logging.DEBUG)

        # Disable request package logger
        logging.getLogger("requests").setLevel(logging.ERROR)

        # Disable warnings
        warnings.filterwarnings("ignore")

        creds = get_nova_creds()
        nova = client.Client(**creds)

        # Upload ssh public key
        key = "{}-qserv".format(creds['username'])
        manage_ssh_key()

        # Find a floating ip for gateway
        floating_ip = get_floating_ip()
        if not floating_ip:
            logging.fatal("Unable to add public ip to Qserv gateway")
            sys.exit(2)

        # Find an image and a flavor to launch an instance
        image = nova.images.find(name="CentOS 7")
        flavor = nova.flavors.find(name="c1.medium")
        
        # Create instances list
        instances = []

        # Create gateway instance and add floating_ip to it
        gateway_id = 0
        gateway_instance = nova_servers_create(gateway_id)
        logging.info("Add floating ip ({0}) to {1}".format(floating_ip,
            gateway_instance.name))
        gateway_instance.add_floating_ip(floating_ip)
        
        # Add gateway to instances list
        instances.append(gateway_instance)

        # Create worker instances
        for instance_id in range(1,2):
            worker_instance = nova_servers_create(instance_id)
            # Add workers to instances list
            instances.append(worker_instance)

        # Show ssh client config
        print_ssh_config(instances, floating_ip)

        #for instance in instances:
        #    nova_servers_delete(instance.name)

    except Exception as exc:
        logging.critical('Exception occured: %s', exc, exc_info=True)
        sys.exit(3)
