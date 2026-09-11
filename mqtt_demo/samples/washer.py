"""Washer descriptor.

Samsung washer / washer-dryer combo (oic.d.washer, DA_WM_TP1_21_COMMON
family). Mirrors the dryer descriptor: the shared /operational/state,
/power, /remotectrl, /energy and /kidslock resources behave identically,
while the washer-specific settings (water temperature, spin, rinse, dry
level, detergent/softener) live under /washer/vs/0.

Captured live on a Samsung WD90DG6B85BK on 2026-09-11.
"""
import time

from ..descriptor import (
    ApplianceDescriptor,
    avail_base,
    avail_with_remote,
    device_block,
    encode,
)
from smartthings_local.ocf.poll_scheduler import PollTier


# --- OBSERVE paths -----------------------------------------------------
# Only Samsung's `/<x>/vs/0` siblings push notifications; the OCF-standard
# `/<x>/0` paths register silently but never fire. flatten() reads the
# /vs/0 strings so a push updates every entity at once.
OBSERVE_PATHS = [
    ['operational', 'state', 'vs', '0'],   # state, remainingTime, progress
    ['power',       'vs', '0'],
    ['remotectrl',  'vs', '0'],
    ['kidslock',    'vs', '0'],
    ['energy',      'consumption', 'vs', '0'],
    ['washer',      'vs', '0'],            # water temp, spin, rinse, dry level
    ['alarms',      'vs', '0'],
]


# --- Static option lists (captured from supported* fields) -------------
# These are model-static, so they seed the HA select dropdowns without a
# live read (discovery is built before the first poll).
WATER_TEMP_OPTIONS = ['None', 'Cold', '20', '30', '40', '60', '90']
SPIN_OPTIONS       = ['RinseHold', 'NoSpin', '400', '800', '1000', '1200', '1400']
RINSE_OPTIONS      = ['0', '1', '2', '3', '4', '5']
DRY_LEVEL_OPTIONS  = ['None', 'Cupboard', '30', '60', '90', '120', '180', '240']


# --- Samsung-state -> OCF currentMachineState --------------------------
_SAMSUNG_STATE_TO_OCF = {
    'Ready':   'idle',
    'Run':     'active',
    'Running': 'active',
    'Pause':   'pause',
    'Paused':  'pause',
    'End':     'idle',
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- flatten -----------------------------------------------------------
def flatten(links):
    """Map the /vs/0 resource dict to the flat sensor dict published to
    MQTT. Every field reads from a `/<x>/vs/0` path so a push update
    drives every entity immediately."""
    g = lambda href, k, default=None: (links.get(href) or {}).get(k, default)

    inst_w = _num(g('/energy/consumption/vs/0',
                    'x.com.samsung.da.instantaneousPower'))
    cum_wh = _num(g('/energy/consumption/vs/0',
                    'x.com.samsung.da.cumulativePower'))
    if inst_w is not None and inst_w < 0:
        # The machine reports a phantom -500W when idle.
        inst_w = 0.0

    sam_state = g('/operational/state/vs/0', 'x.com.samsung.da.state')
    machine_state = (_SAMSUNG_STATE_TO_OCF.get(sam_state, sam_state)
                     if sam_state is not None
                     else g('/operational/state/0', 'currentMachineState'))

    progress = g('/operational/state/vs/0', 'x.com.samsung.da.progress')
    if progress in (None, 'None'):
        progress = 'Idle'

    remaining = (g('/operational/state/vs/0',
                   'x.com.samsung.da.remainingTime')
                 or g('/operational/state/0', 'remainingTime'))
    rem_min = None
    if remaining:
        try:
            h, m, s = remaining.split(':')
            rem_min = int(h) * 60 + int(m) + (1 if int(s) > 0 else 0)
        except Exception:
            pass

    sam_power = g('/power/vs/0', 'x.com.samsung.da.power')
    sam_kids  = g('/kidslock/vs/0', 'x.com.samsung.da.kidsLock')
    sam_rc    = g('/remotectrl/vs/0',
                  'x.com.samsung.da.remoteControlEnabled')
    power_bin = (sam_power == 'On') if sam_power is not None else None
    kids_bin  = (sam_kids != 'Ready') if sam_kids is not None else None
    rc_bin    = (str(sam_rc).lower() == 'true') if sam_rc is not None else None

    return {
        'machine_state':         machine_state,
        'job_state':             progress,
        'progress':              progress,
        'progress_percentage':   _int(g('/operational/state/vs/0',
                                        'x.com.samsung.da.progressPercentage')),
        'completion_time':       remaining,
        'completion_minutes':    rem_min,
        'delay_end_time':        g('/operational/state/vs/0',
                                   'x.com.samsung.da.delayEndTime'),
        'power_state':           sam_power,
        'power_state_binary':    power_bin,
        'child_lock':            sam_kids,
        'child_lock_binary':     kids_bin,
        'remote_control':        sam_rc,
        'remote_control_binary': rc_bin,
        'power_watts':           inst_w,
        'energy_kwh':            round(cum_wh / 1000.0, 2)
                                    if cum_wh is not None else None,
        'energy_wh_cumulative':  int(cum_wh) if cum_wh is not None else None,
        'water_temperature':     g('/washer/vs/0',
                                   'x.com.samsung.da.waterTemperature'),
        'spin_level':            g('/washer/vs/0',
                                   'x.com.samsung.da.spinLevel'),
        'rinse_cycles':          g('/washer/vs/0',
                                   'x.com.samsung.da.rinseCycles'),
        'dry_level':             g('/washer/vs/0',
                                   'x.com.samsung.da.dryLevel'),
        'detergent_level':       g('/washer/vs/0',
                                   'x.com.samsung.da.detergentLevel'),
        'softener_level':        g('/washer/vs/0',
                                   'x.com.samsung.da.softenerLevel'),
    }


# --- Remaining-time anchor + extrapolation -----------------------------
# The machine pushes /operational/state/vs/0 on transitions, not on each
# remainingTime tick. Anchor at the last push; project downward while
# active.
def on_observation(state, href, rep):
    if href != '/operational/state/vs/0':
        return
    rem = rep.get('x.com.samsung.da.remainingTime')
    if not isinstance(rem, str):
        return
    try:
        h, m, s = rem.split(':')
        state['remaining_anchor'] = (time.time(),
                                     int(h) * 3600 + int(m) * 60 + int(s))
    except (ValueError, AttributeError):
        pass


def project(state, sensors):
    anchor = state.get('remaining_anchor')
    if sensors.get('machine_state') != 'active' or anchor is None:
        return sensors
    ts, total = anchor
    remaining = max(0, int(total - (time.time() - ts)))
    h, rest = divmod(remaining, 3600)
    m, s = divmod(rest, 60)
    sensors = dict(sensors)
    sensors['completion_time'] = f"{h}:{m:02d}:{s:02d}"
    sensors['completion_minutes'] = h * 60 + m + (1 if s > 0 else 0)
    return sensors


def log_state_change(sensors):
    return (f"machine={sensors.get('machine_state')} "
            f"progress={sensors.get('progress')} "
            f"remaining={sensors.get('completion_time')} "
            f"power={sensors.get('power_watts')}W")


# --- HA discovery ------------------------------------------------------
MODEL = 'OCF washer (TizenRT-iotivity)'

# (key, friendly name, extra-config-dict)
_SENSORS = [
    ('machine_state',       'Machine state',       {'icon': 'mdi:washing-machine'}),
    ('progress',            'Progress',            {}),
    ('progress_percentage', 'Progress percent',
        {'unit_of_measurement': '%', 'state_class': 'measurement'}),
    ('completion_time',     'Completion time',     {'icon': 'mdi:timer-sand'}),
    ('completion_minutes',  'Remaining minutes',
        {'unit_of_measurement': 'min', 'device_class': 'duration',
         'state_class': 'measurement'}),
    ('delay_end_time',      'Delay end time',      {'icon': 'mdi:timer'}),
    ('power_state',         'Power state',         {}),
    ('power_watts',         'Power',
        {'unit_of_measurement': 'W', 'device_class': 'power',
         'state_class': 'measurement'}),
    ('energy_kwh',          'Energy',
        {'unit_of_measurement': 'kWh', 'device_class': 'energy',
         'state_class': 'total_increasing'}),
    ('water_temperature',   'Water temperature',   {'icon': 'mdi:thermometer'}),
    ('spin_level',          'Spin level',          {'icon': 'mdi:rotate-3d-variant'}),
    ('rinse_cycles',        'Rinse cycles',        {'icon': 'mdi:water'}),
    ('dry_level',           'Dry level',           {'icon': 'mdi:tumble-dryer'}),
    ('detergent_level',     'Detergent level',     {'icon': 'mdi:cup-water'}),
    ('softener_level',      'Softener level',      {'icon': 'mdi:cup-water'}),
]

# (key, friendly name, value_template, device_class)
_BINARY_SENSORS = [
    ('running', 'Running',
        "{{ 'ON' if value_json.machine_state == 'active' else 'OFF' }}",
        'running'),
    ('power_switch', 'Power switch',
        "{{ 'ON' if value_json.power_state_binary else 'OFF' }}",
        'power'),
    ('child_lock_active', 'Child lock',
        "{{ 'ON' if value_json.child_lock_binary else 'OFF' }}",
        'lock'),
    ('remote_control_enabled', 'Remote control',
        "{{ 'ON' if value_json.remote_control_binary else 'OFF' }}",
        'connectivity'),
]

# MQTT command-topic suffixes. The bridge subscribes to <prefix>/cmd/#.
CMD_OPERATIONAL = 'cmd/operational_state'
CMD_WATER_TEMP  = 'cmd/water_temperature'
CMD_SPIN        = 'cmd/spin_level'
CMD_RINSE       = 'cmd/rinse_cycles'
CMD_DRY_LEVEL   = 'cmd/dry_level'

# (cmd-suffix, friendly name, value_template key, options, icon)
_SELECTS = [
    (CMD_WATER_TEMP, 'Water temperature', 'water_temperature',
        WATER_TEMP_OPTIONS, 'mdi:thermometer'),
    (CMD_SPIN,       'Spin level',        'spin_level',
        SPIN_OPTIONS,      'mdi:rotate-3d-variant'),
    (CMD_RINSE,      'Rinse cycles',      'rinse_cycles',
        RINSE_OPTIONS,     'mdi:water'),
    (CMD_DRY_LEVEL,  'Dry level',         'dry_level',
        DRY_LEVEL_OPTIONS, 'mdi:tumble-dryer'),
]


def build_discovery(topic_prefix, ha_prefix, device_name):
    """Return list of (discovery_topic, payload_bytes) tuples, published
    retained on MQTT connect."""
    state_topic   = f"{topic_prefix}/state"
    avail_topic   = f"{topic_prefix}/availability"
    remote_topic  = f"{topic_prefix}/remote_available"
    dev = device_block(topic_prefix, device_name, MODEL)
    out = []

    for key, name, extra in _SENSORS:
        cfg = {
            'name':           name,
            'unique_id':      f"{topic_prefix}_{key}",
            'object_id':      f"{topic_prefix}_{key}",
            'state_topic':    state_topic,
            'value_template': f"{{{{ value_json.{key} }}}}",
            'availability':   avail_base(avail_topic),
            'device':         dev,
        }
        cfg.update(extra)
        out.append((f"{ha_prefix}/sensor/{topic_prefix}/{key}/config",
                    encode(cfg)))

    for key, name, template, dclass in _BINARY_SENSORS:
        cfg = {
            'name':           name,
            'unique_id':      f"{topic_prefix}_{key}",
            'object_id':      f"{topic_prefix}_{key}",
            'state_topic':    state_topic,
            'value_template': template,
            'payload_on':     'ON',
            'payload_off':    'OFF',
            'device_class':   dclass,
            'availability':   avail_base(avail_topic),
            'device':         dev,
        }
        out.append((f"{ha_prefix}/binary_sensor/{topic_prefix}/{key}/config",
                    encode(cfg)))

    # buttons: Start / Pause / Stop (gated on remote control)
    buttons = [
        ('start', 'Start cycle', 'Run',   'mdi:play'),
        ('pause', 'Pause cycle', 'Pause', 'mdi:pause'),
        ('stop',  'Stop cycle',  'Ready', 'mdi:stop'),
    ]
    for key, name, payload_press, icon in buttons:
        cfg = {
            'name':              name,
            'unique_id':         f"{topic_prefix}_{key}",
            'object_id':         f"{topic_prefix}_{key}",
            'command_topic':     f"{topic_prefix}/{CMD_OPERATIONAL}",
            'payload_press':     payload_press,
            'icon':              icon,
            'availability':      avail_with_remote(avail_topic, remote_topic),
            'availability_mode': 'all',
            'device':            dev,
        }
        out.append((f"{ha_prefix}/button/{topic_prefix}/{key}/config",
                    encode(cfg)))

    # selects: water temperature / spin / rinse / dry level
    # (gated on remote control)
    for cmd_suffix, name, key, options, icon in _SELECTS:
        cfg = {
            'name':              name,
            'unique_id':         f"{topic_prefix}_{key}_select",
            'object_id':         f"{topic_prefix}_{key}_select",
            'state_topic':       state_topic,
            'value_template':    f"{{{{ value_json.{key} }}}}",
            'command_topic':     f"{topic_prefix}/{cmd_suffix}",
            'options':           options,
            'icon':              icon,
            'availability':      avail_with_remote(avail_topic, remote_topic),
            'availability_mode': 'all',
            'device':            dev,
        }
        out.append((f"{ha_prefix}/select/{topic_prefix}/{key}/config",
                    encode(cfg)))

    return out


# --- MQTT command handlers ---------------------------------------------
def command_handlers():
    """topic_suffix -> fn(payload, links) -> (path_segs, body_dict) | None.

    None means refuse the command (caller logs & drops)."""
    def _operational(p, _links):
        if p not in ('Run', 'Pause', 'Ready'):
            return None
        return ['operational', 'state', 'vs', '0'], {'x.com.samsung.da.state': p}

    def _make_washer_setter(field, options):
        def _set(p, _links):
            if p not in options:
                return None
            return ['washer', 'vs', '0'], {field: p}
        return _set

    return {
        CMD_OPERATIONAL: _operational,
        CMD_WATER_TEMP:  _make_washer_setter(
            'x.com.samsung.da.waterTemperature', WATER_TEMP_OPTIONS),
        CMD_SPIN:        _make_washer_setter(
            'x.com.samsung.da.spinLevel', SPIN_OPTIONS),
        CMD_RINSE:       _make_washer_setter(
            'x.com.samsung.da.rinseCycles', RINSE_OPTIONS),
        CMD_DRY_LEVEL:   _make_washer_setter(
            'x.com.samsung.da.dryLevel', DRY_LEVEL_OPTIONS),
    }


# --- Poll tiers --------------------------------------------------------
WASHER_POLL_TIERS = [
    PollTier(
        name='hot',
        interval_s=1.0,
        active_interval_s=0.5,
        timeout_s=2.0,
        paths=(
            ('operational', 'state', 'vs', '0'),
        ),
    ),
    PollTier(
        name='warm',
        interval_s=15.0,
        timeout_s=4.0,
        paths=(
            ('power', 'vs', '0'),
            ('remotectrl', 'vs', '0'),
            ('kidslock', 'vs', '0'),
            ('washer', 'vs', '0'),
            ('energy', 'consumption', 'vs', '0'),
            ('alarms', 'vs', '0'),
        ),
    ),
    PollTier(
        name='sweep',
        interval_s=300.0,
        timeout_s=15.0,
        paths=(('device', '0'),),
        is_sweep=True,
    ),
]


def _is_active(links: dict) -> bool:
    rep = links.get('/operational/state/vs/0') or {}
    sam_state = rep.get('x.com.samsung.da.state')
    return _SAMSUNG_STATE_TO_OCF.get(sam_state) == 'active'


# --- Descriptor --------------------------------------------------------
WASHER = ApplianceDescriptor(
    name='washer',
    default_observe_port=49154,
    observe_paths=OBSERVE_PATHS,
    seed_path=['device', '0'],
    flatten=flatten,
    build_discovery=build_discovery,
    command_handlers=command_handlers,
    on_observation=on_observation,
    project=project,
    remote_available_field='remote_control_binary',
    log_state_change=log_state_change,
    poll_tiers=WASHER_POLL_TIERS,
    is_active=_is_active,
)
