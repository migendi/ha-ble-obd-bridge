import asyncio, json, logging, re, time
from pathlib import Path
from bleak import BleakScanner, BleakClient
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG=logging.getLogger("veepeak")
OPT=json.loads(Path('/data/options.json').read_text())
ADDR=OPT.get('ble_address','').strip().upper()
if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', ADDR):
    raise SystemExit('Set ble_address to the Bluetooth address of your OBD adapter.')
BLE_PROFILE=OPT.get('ble_profile','auto')
BLE_PROFILES={
    'fff0': (
        '0000fff0-0000-1000-8000-00805f9b34fb',
        '0000fff2-0000-1000-8000-00805f9b34fb',
        '0000fff1-0000-1000-8000-00805f9b34fb',
    ),
    'ffe0': (
        '0000ffe0-0000-1000-8000-00805f9b34fb',
        '0000ffe1-0000-1000-8000-00805f9b34fb',
        '0000ffe1-0000-1000-8000-00805f9b34fb',
    ),
    'nordic_uart': (
        '6e400001-b5a3-f393-e0a9-e50e24dcca9e',
        '6e400002-b5a3-f393-e0a9-e50e24dcca9e',
        '6e400003-b5a3-f393-e0a9-e50e24dcca9e',
    ),
}
PREFIX=OPT.get('mqtt_prefix','emma/obd').rstrip('/')
POLL=max(2,int(OPT.get('poll_seconds',5)))

if not OPT.get('mqtt_username'):
    import os, urllib.request
    req=urllib.request.Request('http://supervisor/services/mqtt',headers={'Authorization':'Bearer '+os.environ['SUPERVISOR_TOKEN']})
    with urllib.request.urlopen(req,timeout=10) as response:
        service=json.load(response)['data']
    OPT.update(mqtt_host=service['host'],mqtt_port=service['port'],mqtt_username=service['username'],mqtt_password=service['password'])
    LOG.info('Using Supervisor MQTT service credentials')

mq=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='emma-veepeak-obd')
if OPT.get('mqtt_username'):
    mq.username_pw_set(OPT['mqtt_username'], OPT.get('mqtt_password',''))
mq.will_set(f'{PREFIX}/status','offline',retain=True)
mq.connect(OPT.get('mqtt_host','core-mosquitto'), int(OPT.get('mqtt_port',1883)), 60)
mq.loop_start()

# Requirement 2026-10-23: preserve every last valid OBD value while ignition is off.
# Retained MQTT + atomic persistent cache; no synthetic zero/offline measurements.
from datetime import datetime, timezone
CACHE=Path('/data/last_values.json')
try:
    LAST=json.loads(CACHE.read_text())
except (OSError,ValueError):
    LAST={}
try:
    legacy=json.loads(Path('/data/fuel_last.json').read_text())
    for key,value in legacy.items():
        LAST.setdefault(key,{'value':value,'measured_at':None})
except (OSError,ValueError):
    pass
for topic in list(LAST):
    if topic == 'fuel_liters' or topic.startswith('adblue_'):
        del LAST[topic]

def persist():
    tmp=CACHE.with_suffix('.tmp')
    tmp.write_text(json.dumps(LAST,ensure_ascii=False))
    tmp.replace(CACHE)

def replay():
    for topic,entry in list(LAST.items()):
        mq.publish(f'{PREFIX}/{topic}',entry['value'],retain=True)
        mq.publish(f'{PREFIX}/{topic}/attributes',json.dumps({'last_valid_measurement':entry.get('measured_at'),'value_policy':'last_valid','validation':('unverified' if topic.startswith('pid_') else 'standard_pid')},ensure_ascii=False),retain=True)

def pub(topic,value,retain=False):
    if topic=='status':
        mq.publish(f'{PREFIX}/{topic}',value,retain=True)
        return
    if value is None or value in ('unknown','unavailable','NO DATA',''):
        return
    stamp=datetime.now(timezone.utc).isoformat()
    LAST[topic]={'value':value,'measured_at':stamp}
    persist()
    mq.publish(f'{PREFIX}/{topic}',value,retain=True)
    mq.publish(f'{PREFIX}/{topic}/attributes',json.dumps({'last_valid_measurement':stamp,'value_policy':'last_valid','validation':('unverified' if topic.startswith('pid_') else 'standard_pid')}),retain=True)

def discovery(obj, name, unit=None, devclass=None, stateclass=None):
    uid=f'emma_veepeak_{obj}'
    p={'name':name,'unique_id':uid,'state_topic':f'{PREFIX}/{obj}','availability_topic':f'{PREFIX}/status','payload_available':'online','payload_not_available':'offline','device':{'identifiers':['emma_veepeak_obd'],'name':'EMMA Veepeak OBD','manufacturer':'Veepeak','model':'OBDCheck BLE'}}
    if True:
        for key in ('availability_topic','payload_available','payload_not_available'):
            p.pop(key,None)
    p['json_attributes_topic']=f'{PREFIX}/{obj}/attributes'
    if obj.startswith('pid_'): p['entity_category']='diagnostic'
    if unit: p['unit_of_measurement']=unit
    if devclass: p['device_class']=devclass
    if stateclass: p['state_class']=stateclass
    mq.publish(f'homeassistant/sensor/{uid}/config',json.dumps(p),retain=True)

def setup_discovery():
    discovery('rpm','Engine speed','rpm',None,'measurement')
    discovery('speed','Vehicle speed','km/h','speed','measurement')
    discovery('coolant','Coolant temperature','°C','temperature','measurement')
    discovery('voltage','ECU voltage','V','voltage','measurement')
    discovery('adapter','OBD adapter identification')

async def ble_target():
    from dbus_fast.aio import MessageBus
    from dbus_fast import BusType, Message, MessageType
    from bleak.backends.device import BLEDevice
    bus=await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply=await bus.call(Message(destination='org.bluez',path='/',interface='org.freedesktop.DBus.ObjectManager',member='GetManagedObjects'))
        if reply.message_type == MessageType.ERROR: raise RuntimeError(str(reply.body))
        for path, interfaces in reply.body[0].items():
            props=interfaces.get('org.bluez.Device1',{})
            if props.get('Address') and props['Address'].value.upper()==ADDR.upper():
                values={k:v.value for k,v in props.items()}
                return BLEDevice(ADDR,values.get('Name','VEEPEAK'),{'path':path,'props':values})
    finally:
        bus.disconnect()
    device=await BleakScanner.find_device_by_address(ADDR,timeout=20)
    if device is None: raise RuntimeError('VEEPEAK not found')
    return device

def select_ble_transport(services,requested):
    candidates=BLE_PROFILES.items() if requested=='auto' else [(requested,BLE_PROFILES[requested])]
    for profile,(service_uuid,tx_uuid,rx_uuid) in candidates:
        for service in services:
            if service.uuid.lower()!=service_uuid:
                continue
            tx=next((ch for ch in service.characteristics
                     if ch.uuid.lower()==tx_uuid and
                     {'write','write-without-response'}.intersection(ch.properties)),None)
            rx=next((ch for ch in service.characteristics
                     if ch.uuid.lower()==rx_uuid and
                     {'notify','indicate'}.intersection(ch.properties)),None)
            if tx is not None and rx is not None:
                return profile,tx,rx,'write-without-response' not in tx.properties
    raise RuntimeError(f'No compatible BLE OBD characteristics for profile {requested}')

class Elm:
    def __init__(self,c):
        self.c=c
        self.profile,self.tx,self.rx_char,self.write_response=select_ble_transport(c.services,BLE_PROFILE)
        self.buf=''
        self.event=asyncio.Event()
    def rx(self,_,data):
        self.buf += data.decode(errors='replace')
        if '>' in self.buf: self.event.set()
    async def cmd(self,s,timeout=20):
        if s not in EXTRA_ALLOWED and s not in {'ATI','ATE0','ATL0','ATS1','ATH0','ATH1','ATDPN','090A','ATSP0','0100','010C','010D','0105','0142'}: raise ValueError('Command outside read-only allowlist')
        self.buf=''; self.event.clear()
        await self.c.write_gatt_char(self.tx,(s+'\r').encode(),response=self.write_response)
        try: await asyncio.wait_for(self.event.wait(),timeout)
        except asyncio.TimeoutError: raise TimeoutError(f'No complete ELM response to {s}: {self.buf!r}')
        r=self.buf.replace('\r','\n').replace('>','').strip()
        LOG.info('ELM %s -> %r',s,r)
        return r

def hexbytes(resp):
    lines=[]
    indexed=None
    for line in resp.upper().splitlines():
        index=re.match(r'^\s*([0-9A-F]+):\s*(.*)$',line)
        if index:
            data=re.findall(r'\b[0-9A-F]{2}\b',index.group(2))
            if int(index.group(1),16)==0 or indexed is None:
                indexed=[]; lines.append(indexed)
            indexed.extend(data)
            continue
        indexed=None
        b=re.findall(r'\b[0-9A-F]{2}\b',line)
        if b: lines.append(b)
    return lines

def mode01(resp,pid):
    target=f'{0x40+1:02X}'; p=f'{pid:02X}'
    for b in hexbytes(resp):
        for i in range(len(b)-2):
            if b[i]==target and b[i+1]==p:
                return [int(x,16) for x in b[i+2:]]
    return None

# Standard Mode 01 only. Scale definitions checked against public PID references.
EXTRA_SPECS = [
 (0x04,'engine_load','Engine load','%',None,1,lambda d:d[0]*100/255),
 (0x0B,'intake_pressure','Intake manifold pressure','kPa','pressure',1,lambda d:d[0]),
 (0x0F,'intake_temperature','Intake air temperature','°C','temperature',1,lambda d:d[0]-40),
 (0x10,'air_flow','Mass airflow','g/s',None,2,lambda d:(d[0]*256+d[1])/100),
 (0x11,'throttle','Throttle position','%',None,1,lambda d:d[0]*100/255),
 (0x1F,'runtime','Engine run time','s','duration',2,lambda d:d[0]*256+d[1]),
 (0x21,'mil_distance','Distance with MIL on','km','distance',2,lambda d:d[0]*256+d[1]),
 (0x2F,'fuel_level','Fuel level','%',None,1,lambda d:d[0]*100/255),
 (0x30,'warmups','Warm-ups since codes cleared',None,None,1,lambda d:d[0]),
 (0x31,'clear_distance','Distance since codes cleared','km','distance',2,lambda d:d[0]*256+d[1]),
 (0x33,'barometric_pressure','Barometric pressure','kPa','pressure',1,lambda d:d[0]),
 (0x46,'ambient_temperature','Ambient temperature','°C','temperature',1,lambda d:d[0]-40),
 (0x49,'accelerator_d','Accelerator pedal position D','%',None,1,lambda d:d[0]*100/255),
 (0x4A,'accelerator_e','Accelerator pedal position E','%',None,1,lambda d:d[0]*100/255),
 (0x4C,'throttle_command','Commanded throttle','%',None,1,lambda d:d[0]*100/255),
 (0x5E,'fuel_rate','Fuel rate','L/h',None,2,lambda d:(d[0]*256+d[1])/20),
 (0x61,'driver_torque','Driver demand torque','%',None,1,lambda d:d[0]-125),
 (0x62,'actual_torque','Actual torque','%',None,1,lambda d:d[0]-125),
 (0x63,'reference_torque','Reference torque','Nm',None,2,lambda d:d[0]*256+d[1]),
 (0x01,'dtc_count','Stored trouble code count',None,None,4,lambda d:d[0]&127),
]
EXTRA_SPECS.extend([
 (0x45,'relative_throttle','Relative throttle position','%',None,1,lambda d:d[0]*100/255),
 (0x4D,'mil_runtime','Run time with MIL on','min','duration',2,lambda d:d[0]*256+d[1]),
 (0x4E,'clear_runtime','Run time since codes cleared','min','duration',2,lambda d:d[0]*256+d[1]),
 (0x50,'maximum_air_flow','Maximaler Mass airflownmessbereich','g/s',None,4,lambda d:d[0]*10),
 (0x51,'fuel_type_code','OBD fuel type code',None,None,1,lambda d:d[0]),
 (0x5C,'oil_temperature','Engine oil temperature','°C','temperature',1,lambda d:None if d[0]==255 else d[0]-40),
])
# Only standard Mode 01. Complex fields remain explicitly raw until validated.
EXTRA_ALLOWED={f'01{x[0]:02X}' for x in EXTRA_SPECS}|{f'01{b:02X}' for b in range(0,225,32)}|{f'01{x:02X}' for x in range(1,256)}


# Field layouts checked against DashLogic J1979 documentation, 2026-10-23.
# mask, object, name, unit, class, byte offset, width, scale, bias, signed
COMPLEX_SPECS={
 0x68:[(1<<i,f'intake_temperature_b{i//3+1}_s{i%3+1}',f'Intake air temperature Bank {i//3+1} Sensor {i%3+1}','°C','temperature',i+1,1,1,-40,False) for i in range(6)],
 0x69:[(1<<i,f'egr_{i//3+1}_{["target","actual","error"][i%3]}',f'EGR {i//3+1} {["target","actual","error"][i%3]}','%',None,i+1,1,100/128 if i%3==2 else 100/255,-100 if i%3==2 else 0,False) for i in range(6)],
 0x6A:[(1<<i,f'intake_flow_{i//2+1}_{["target","actual"][i%2]}',f'Intake airflow control {i//2+1} {["target","actual"][i%2]}','%',None,i+1,1,100/255,0,False) for i in range(4)],
 0x78:[(1<<i,f'egt_b1_s{i+1}',f'Exhaust gas temperature Bank 1 Sensor {i+1}','°C','temperature',1+2*i,2,0.1,-40,False) for i in range(4)],
 0x7A:[
 (1,'dpf_b1_differential_pressure','DPF differential pressure Bank 1','kPa','pressure',1,2,0.01,0,True),
 (2,'dpf_b1_inlet_pressure','DPF inlet pressure Bank 1','kPa','pressure',3,2,0.01,0,False),
 (4,'dpf_b1_outlet_pressure','DPF outlet pressure Bank 1','kPa','pressure',5,2,0.01,0,False)],
 0x83:[(1<<i,f'nox_b{i//2+1}_s{i%2+1}',f'NOx Bank {i//2+1} Sensor {i%2+1}','ppm',None,1+2*i,2,1,0,False) for i in range(4)],
 0x87:[(1<<i,f'intake_pressure_{i+1}',f'Intake manifold pressure Sensor {i+1}','kPa','pressure',1+2*i,2,1/32,0,False) for i in range(2)]
}
def complex_values(pid,d):
    out=[]
    for mask,obj,name,unit,dc,pos,width,scale,bias,signed in COMPLEX_SPECS.get(pid,[]):
        if not d or not d[0]&mask or len(d)<pos+width: continue
        b=d[pos:pos+width]
        if all(x==255 for x in b): continue
        raw=int.from_bytes(bytes(b),'big',signed=signed)
        out.append((obj,name,unit,dc,round(raw*scale+bias,2)))
    return out

def supported_mask(resp,base):
    mask=0
    for b in hexbytes(resp):
        if len(b)>=6 and b[:2]==['41',f'{base:02X}']:
            mask |= int(''.join(b[2:6]),16)
    return mask

async def find_supported(e,first):
    found=set()
    for base in range(0,225,32):
        raw=first if base==0 else await e.cmd(f'01{base:02X}')
        mask=supported_mask(raw,base)
        found.update(base+i+1 for i in range(32) if mask & (1<<(31-i)))
        if not mask&1: break
    LOG.info('Supported Mode 01 PIDs: %s',','.join(f'{p:02X}' for p in sorted(found)))
    return found

async def poll_extra(e,supported,known):
    def emit(obj,name,unit,devclass,value):
        if obj not in known:
            discovery(obj,name,unit,devclass,'measurement' if isinstance(value,(int,float)) else None)
            known.add(obj)
        pub(obj,round(value,2) if isinstance(value,float) else value)
    for pid,obj,name,unit,devclass,length,decode in EXTRA_SPECS:
        if pid not in supported: continue
        d=mode01(await e.cmd(f'01{pid:02X}'),pid)
        if d is None or len(d)<length: continue
        value=decode(d)
        if value is None: continue
        emit(obj,name,unit,devclass,value)


def mqtt_reconnected(client,userdata,flags,reason_code,properties):
    if not reason_code.is_failure:
        replay()
mq.on_connect=mqtt_reconnected


# Flight recorder revision 20261112. Only the existing ELM allowlist is used.
from logging.handlers import RotatingFileHandler
import uuid
RECORDER_REVISION='20261112-flight-1'
SESSION=uuid.uuid4().hex
flight_logger=logging.getLogger('veepeak.flight')
flight_logger.propagate=False
flight_logger.setLevel(logging.INFO)
Path('/share/OBD_Captures').mkdir(parents=True,exist_ok=True)
flight_handler=RotatingFileHandler('/share/OBD_Captures/obd_flight.jsonl',maxBytes=8*1024*1024,backupCount=7,encoding='utf-8')
flight_handler.setFormatter(logging.Formatter('%(message)s'))
flight_logger.addHandler(flight_handler)
def record(kind, **data):
    flight_logger.info(json.dumps(dict(revision=RECORDER_REVISION,session=SESSION,
        timestamp=datetime.now(timezone.utc).isoformat(),monotonic=time.monotonic(),
        kind=kind,**data),ensure_ascii=False))

# Record exact text (including original ELM line breaks), not decoded retained values.
_original_cmd=Elm.cmd
async def recorded_cmd(self,command,timeout=20):
    await asyncio.sleep(max(0,0.2-(time.monotonic()-getattr(self,'last_command_end',0))))
    started=time.monotonic()
    try:
        response=await _original_cmd(self,command,timeout)
    except Exception as error:
        record('command_error',command=command,headers=getattr(self,'headers',False),
            raw=getattr(self,'buf',''),error=str(error),duration=time.monotonic()-started)
        # A timed-out ELM response can arrive late. Force reconnect, no further commands.
        raise
    finally:
        self.last_command_end=time.monotonic()
    addresses=[]
    if getattr(self,'headers',False) and not command.startswith('AT'):
        for line in response.upper().splitlines():
            match=re.match(r'^\s*([0-9A-F]{8}|[0-9A-F]{3})(?=\s)',line)
            if match: addresses.append(match.group(1))
            else:
                match=re.match(r'^\s*((?:[0-9A-F]{2}\s+){3}[0-9A-F]{2})\s+',line)
                if match: addresses.append(re.sub(r'\s+','',match.group(1)))
    record('response',command=command,headers=getattr(self,'headers',False),
        ecu_addresses=sorted(set(addresses)),response=response,
        raw=getattr(self,'buf',''),duration=time.monotonic()-started)
    if command in ('ATH1','ATH0'):
        if 'OK' not in response.upper(): raise RuntimeError('ELM rejected '+command+': '+response)
        self.headers=command=='ATH1'
    return response
Elm.cmd=recorded_cmd

async def capture_ecu_sources(e):
    # Once per BLE connection; bounded standard emissions inventory, no UDS scanning.
    record('vehicle_connection',note='Fresh valid OBD response; ignition state not independently known')
    await e.cmd('ATH1',timeout=5)
    await e.cmd('ATDPN',timeout=5)
    supported=set()
    for base in range(0,225,32):
        response=await e.cmd(f'01{base:02X}',timeout=5)
        mask=0
        for values in hexbytes(response):
            for index in range(len(values)-5):
                if values[index:index+2]==['41',f'{base:02X}']:
                    mask |= int(''.join(values[index+2:index+6]),16)
        supported.update(base+i+1 for i in range(32) if mask & (1<<(31-i)))
        if not mask&1: break
    await e.cmd('090A',timeout=5)
    await e.cmd('ATH0',timeout=5)
    e.flight_candidates=[f'01{pid:02X}' for pid in (0x83,0x85,0x88,0x9B)
                        if pid in supported or pid in (0x85,0x9B)]
    e.flight_index=0
    e.flight_next=time.monotonic()+15
    record('inventory_complete',supported_pids=[f'{pid:02X}' for pid in sorted(supported)],
        candidates=e.flight_candidates,note='85/9B also probed because prior EMMA responses exist; no AdBlue scaling')

async def flight_tick(e):
    if time.monotonic()<e.flight_next or not e.flight_candidates: return
    command=e.flight_candidates[e.flight_index]
    await e.cmd('ATH1',timeout=5)
    await e.cmd(command,timeout=5)
    await e.cmd('ATH0',timeout=5)
    e.flight_index=(e.flight_index+1)%len(e.flight_candidates)
    e.flight_next=time.monotonic()+15


async def main():
    LOG.info("FLIGHT_RECORDER_READY revision=%s path=/share/OBD_Captures/obd_flight.jsonl",RECORDER_REVISION)
    record("reader_start",retained_values_are_historical=True)
    setup_discovery()
    for fields in COMPLEX_SPECS.values():
        for mask,obj,name,unit,dc,pos,width,scale,bias,signed in fields:
            if obj in LAST: discovery(obj,name,unit,dc,'measurement')
    for obj in LAST:
        if obj.startswith('pid_'): discovery(obj,'PID '+obj.split('_')[1].upper()+' unverified raw data')
    replay()
    for pid,obj,name,unit,devclass,length,decode in EXTRA_SPECS:
        discovery(obj,name,unit,devclass,'measurement')
    pub('status','offline',True)
    while True:
        try:
            LOG.info('Looking for Veepeak %s',ADDR)
            async with BleakClient(await ble_target(), timeout=25) as c:
                LOG.info("Direct BLE connection: %s", ADDR)
                LOG.info('BLE connected: %s',ADDR)
                e=Elm(c)
                LOG.info('BLE profile: %s',e.profile)
                await c.start_notify(e.rx_char,e.rx)
                ati=await e.cmd('ATI'); pub('adapter',ati.replace('\n',' ') or 'unknown',True)
                await e.cmd('ATE0'); await e.cmd('ATL0'); await e.cmd('ATS1'); await e.cmd('ATH0'); await e.cmd('ATSP0')
                supported=await e.cmd('0100',timeout=20); LOG.info('Supported PIDs: %r',supported)
                if mode01(supported,0x00) is None: raise RuntimeError('Vehicle ECU unavailable: '+supported)
                await capture_ecu_sources(e)
                supported_pids=await find_supported(e,supported)
                known=set(); next_extra=0
                raw_pids=[p for p in sorted(supported_pids) if p%32 and p not in {x[0] for x in EXTRA_SPECS} and p not in {5,12,13,66}]
                raw_index=0; next_raw=0
                pub('status','online',True)
                while c.is_connected:
                    d=mode01(await e.cmd('010C'),0x0C)
                    if d and len(d)>=2: pub('rpm',round((d[0]*256+d[1])/4,1))
                    d=mode01(await e.cmd('010D'),0x0D)
                    if d: pub('speed',d[0])
                    d=mode01(await e.cmd('0105'),0x05)
                    if d: pub('coolant',d[0]-40)
                    d=mode01(await e.cmd('0142'),0x42)
                    if d and len(d)>=2: pub('voltage',round((d[0]*256+d[1])/1000,3))
                    if time.monotonic()>=next_extra:
                        await poll_extra(e,supported_pids,known)
                        next_extra=time.monotonic()+30
                    if raw_pids and time.monotonic()>=next_raw:
                        pid=raw_pids[raw_index]
                        raw=await e.cmd(f'01{pid:02X}')
                        payload=mode01(raw,pid)
                        if payload:
                            for obj,name,unit,dc,value in complex_values(pid,payload):
                                discovery(obj,name,unit,dc,'measurement')
                                pub(obj,value)
                            obj=f'pid_{pid:02x}_raw'
                            discovery(obj,f'PID {pid:02X} unverified raw data')
                            pub(obj,' '.join(f'{b:02X}' for b in payload)[:250])
                            mq.publish(f'{PREFIX}/{obj}/response',json.dumps({'pid':f'{pid:02X}','response':raw,'measured_at':datetime.now(timezone.utc).isoformat()}),retain=True)
                        raw_index=(raw_index+1)%len(raw_pids)
                        next_raw=time.monotonic()+(POLL if raw_index else 300)
                    await flight_tick(e)
                    await asyncio.sleep(POLL)
        except Exception as ex:
            LOG.exception('Veepeak-Reader: %s',ex); pub('status','offline',True); await asyncio.sleep(10)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        pub('status', 'offline', True)
        mq.loop_stop()
        mq.disconnect()
