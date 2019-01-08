"""Monitoring library using gNMI GetRequests.

Current version does not perform Subscribe.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import collections
import json
import sys
import time
import gnmi_lib
import configs_lib
from influxdb import InfluxDBClient
from absl import logging
import pyangbind.lib.pybindJSON as pybindJSON
from absl import app
from absl import flags
import constants

FLAGS = flags.FLAGS

flags.DEFINE_enum('mode', None, ['configure', 'monitor', 'provision',],
                  '\n'
                  'configure: Update or create configuration for AP.\n'
                  'provision : Generate OC config to apply hostname/cc.\n'
                  'monitor : Perform monitoring of Config & State Telemetry.\n')
flags.DEFINE_bool('dry_run', False, 'Generate OpenConfig JSON, print and exit.\n')


class ApObject(object):
  """Represents an access point."""

  def __init__(self, ap_name):
    """Initializes a AP Object with given attributes.

    Args:
      ap_name: (str) name of the access point.
    Attributes:
      ssids: (list) of SSIDs this AP is to broadcast.
      targetip: (str) gNMI Target IP for this AP.
      targetport: (str) gNMI Target port for this AP.
      targetuser: (str) username for gRPC Metadata authentication.
      targetpass: (str) password for gRPC Metadata authentication.
      mac: (str) Mac address of this AP.
      stub: (gNMIStub) gNMI Stub object for this AP.
      json: (dict) JSON payload for a gNMI GetRequest.
    """
    self.ap_name = ap_name
    self.ssids = []
    self.targetip = None
    self.targetport = None
    self.targetuser = None
    self.targetpass = None
    self.mac = None
    self.stub = None
    self.json = None


def _get_grpcmetadata(ap):
  """Helper to determine gRPC metadata needed for secure_channel.

  Args:
    ap: AP Class object.
  """
  for vendor, oui in constants.VENDOR_OUI_DATA.iteritems():
    if ':'.join(ap.mac.split(':')[:3]) in oui:
      if vendor == 'mist':
        ap.targetuser = constants.MIST_USER
        ap.targetpass = constants.MIST_PASS
      elif vendor == 'arista':
        ap.targetuser = constants.ARISTA_USER
        ap.targetpass = constants.ARISTA_PASS


def _create_apobj(gnmi_target, ap_name, ap_mac, student_ssids):
  """Creates a list of AP objects.

  Args:
    gnmi_target: (str) gNMI Target IP/FQDN
    ap_name: (str) Access Point hostname.
    ap_mac: (str) Access Point MAC address.
    student_ssids: (list) SSIDs to configure on the AP.
  Returns:
    ap: Access Point class object with meta.
  """
  ap = ApObject(ap_name)
  ap.mac = ap_mac
  ap.targetip = gnmi_target.split(':')[0]
  ap.targetport = gnmi_target.split(':')[1]
  ap.pskssid = student_ssids[1]
  ap.openssid = student_ssids[0]
  _get_grpcmetadata(ap)
  return ap


def _create_db():
  """Create a database if one does not already exist.

  Returns
    client: InfluxDBClient
  """
  dbclient = InfluxDBClient('localhost', 8086, 'root', 'root')
  # Check for existence of aps DB
  existing = dbclient.get_list_database()
  dbnames = [db['name'] for db in existing]
  if 'ap_telemetry' not in dbnames:
    dbclient.create_database('ap_telemetry')
  return dbclient


def _get(ap, path):
  """Get OpenConfig JSON.

  Args:
    ap: AP Class Object.
    path: (str) Describing the xpath.
  Returns:
    config_json: Full AP config in OC IETF_JSON.
  """
  if path == 'r0-config':
    paths = gnmi_lib.ParsePath(gnmi_lib.PathNames(
        '/access-points/access-point[hostname=%s]/radios/radio[id=0]/config' %
        ap.ap_name))
  elif path == 'r0-state':
    paths = gnmi_lib.ParsePath(gnmi_lib.PathNames(
        '/access-points/access-point[hostname=%s]/radios/radio[id=0]/state/' %
        ap.ap_name))
  if ap.targetport == '443' or ap.targetport == '10161':
  # Target is ap-manager.
    response = gnmi_lib.Get(
      ap.stub, paths, constants.MIST_USER, constants.MIST_PASS)
    return json.loads(response.notification[0].update[0].val.json_ietf_val)  #TODO: Change this to json.dumps (then shouldnt have to dump later in _preo_json)
  response = gnmi_lib.Get(  # Target is AP.
    ap.stub, paths, constants.ARISTA_USER, constants.ARISTA_PASS)
  return json.loads(response.notification[0].update[0].val.json_ietf_val)


def _write_db(dbclient, db, json_data):
  """Write JSON to DB.

  Args:
    dbclient: InfluxDB Client.
    db: (str) Database to write to.
    json_data: (dict) JSON data to write to DB.
  """
  dbclient.switch_database(db)
  dbwrite = dbclient.write_points(json_data)
  if not dbwrite:
    logging.error('DB write failed')
  else:
    logging.info('write successful')


def _prep_json(json_data, measurement, ap):
  """Prepare the JSON for DB write.

  Args:
    json_data: Dict of raw JSON.
    measurement: (str) DB measurement we are writing to.
    ap: AP Class object.
  Returns:
    dbjson: Dict of DB writable JSON.
  """
  if isinstance(json_data, dict):
    return [{"measurement": measurement, "tags": {"ap_name": ap.ap_name}, "fields":
             {"value":json.dumps(json_data)}}]  # If already dict, do I need to dump?
  return [{"measurement": measurement, "tags":
           {"ap_name": ap.ap_name}, "fields": {"value": json_data}}]


def _config_diff(dbclient, db, ap):
  """Compare config State to intent.

  This func writes OK or Out-of-sync
  for each Access Point.

  Args:
    dbclient: InfluxDB Client.
    db: InfluxDB Database.
    ap: AP Class object.
  """
  dbclient.switch_database(db)
  state = dbclient.query(
      'select last(value) from "config_state" where ap_name=\'%s\'' %
      ap.ap_name)
  intent = dbclient.query(
      'select last(value) from "config_intent" where ap_name=\'%s\'' %
      ap.ap_name)
  state_raw = state.raw['series'][0]['values'][0][1]
  intent_raw = intent.raw['series'][0]['values'][0][1]
  if state_raw == intent_raw:  # Config in sync.
    _write_db(dbclient, 'ap_telemetry', _prep_json(2, 'conf_sync'))
  else:  # Config out of sync.
    _write_db(dbclient, 'ap_telemetry', _prep_json(1, 'conf_sync'))


def main(unused_argv):
  if not FLAGS.mode:
    print(constants.USAGE)
    sys.exit()
  gnmi_target = '192.168.1.50:8080'  # This is target IP/FQDN:TCP_PORT.
  ap_name = 'tester-01-albano.example.com'  # This is the AP FQDN you want to use.
  ap_mac = '00:11:74:87:C0:7F'  # You know what this is.
  # Change the following if you want. The first one will be 'open', second 'psk'
  student_ssids = ['student1_open', 'student1_psk']
  ap = _create_apobj(gnmi_target, ap_name, ap_mac, student_ssids)
  if not FLAGS.dry_run:
    configs_lib.GnmiSetUp(ap)  # Set up gNMI for each AP.
  if FLAGS.mode.lower() == 'provision':
    configs_lib.Provision(ap)
  if FLAGS.mode.lower() == 'configure':
    # Applies configuration and returns the full JSON blob for DB write.
    config_json = configs_lib.ConfigPhyMac(ap, student_ssids)
    dbclient = _create_db()  # Create DB and dbclient.
    dbjson = _prep_json(config_json, 'full_config', ap)  # Prep it for DB write.
    _write_db(dbclient, 'ap_telemetry', dbjson)  # Write Config JSON to DB.
  if FLAGS.mode.lower() == 'monitor':
    dbclient = _create_db()  # Create DB and dbclient.
    while True:
      radio0_state = _get(ap, 'r0-state')  # Get radio 0 State JSON
      dbjson = _prep_json(radio0_state, 'radio0_state')  # Prep it for DB write.
      _write_db(dbclient, 'ap_telemetry', dbjson)  # Write State JSON to DB.
      _config_diff(dbclient, 'ap_telemetry', ap)  # Compare State Vs Intent.
      # Get radio 0 channel utilization
      cu_state = _get(ap, 'r0-state')['openconfig-access-points:total-channel-utilization']
      dbjson = _prep_json(cu_state, 'channel_utilization')  # Prep it for DB write.
      _write_db(dbclient, 'ap_telemetry', dbjson)  # Write config State JSON to DB.
      time.sleep(5)

if __name__ == '__main__':
  app.run(main)
