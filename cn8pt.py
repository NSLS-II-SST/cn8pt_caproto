from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run
from caproto import ChannelType
import asyncio
from textwrap import dedent

si1_lookup = {0: {0: "J", 1: "K", 2: "T", 3: "E", 4: "N", 6: "R", 7: "S", 8: "B", 9: "C"},
              1: {0: "2 Wire", 1: "3 Wire", 2: "4 Wire"},
              2: {0: "4-20 mA", 1: "0-24 mA", 5: "+/- 10 Vdc", 6: "+/- 1.0 Vdc", 7: "+/- 0.1 Vdc"},
              3: {0: "2.25 K", 1: "5 K", 2: "10 K"}}
si2_lookup = {1: {0: "385 Curve, 100 ohms", 1: "385 Curve, 500 ohms", 2: "385 Curve, 1000 ohms",
                  3: "392 Curve, 100 ohms", 4: "3916 Curve, 100 ohms"},
              2: {0: "Live", 1: "Manual"}}
output_type_lookup = {"000": "No Output", "001": "Single Poll Relay", "002": "SSR output",
                      "004": "Double Poll Relay", "008": "DC Pulse output", "010": "Analog Output",
                      "020": "Isolated Analog Output"}

class CN8PT(PVGroup):
    """
    A class to control Omega CN8PT temperature controllers
    """
    
    temperature = pvproperty(value=0.0, record='ai', doc="temperature")
    sensor = pvproperty(value="",
                        enum_strings=("thermocouple",
                                      "rtd",
                                      "process input",
                                      "thermistor"),
                        record="mbbi",
                        dtype=ChannelType.ENUM,
                        doc="The sensor type of the contoller")
    si1 = pvproperty(value="", dtype=str, report_as_string=True, doc="Sensor info 1")
    si2 = pvproperty(value="", dtype=str, report_as_string=True, doc="Sensor info 2")
    setpoint = pvproperty(value=0.0, record='ai', doc="control setpoint")
    output_chan = pvproperty(value=3, doc="output channel")
    output_mode = pvproperty(value="off", enum_strings=("off",
                                                        "pid",
                                                        "on-off",
                                                        "retransmit",
                                                        "alarm1",
                                                        "alarm2",
                                                        "ramp soak RE",
                                                        "ramp soak SE"),
                             record="mbbi",
                             dtype=ChannelType.ENUM,
                             doc="output mode")
    output_range = pvproperty(value=0, enum_strings=("0-10V", "0-5V", "0-20V", "4-20V", "0-24V"),
                              record="mbbi",
                              dtype=ChannelType.ENUM,
                              doc="Output range")
    output_type = pvproperty(value="No Output", report_as_string=True, dtype=str)
    pid_hi_lim = pvproperty(value=100, doc="maximum output percentage")

    def __init__(self, *args, address="10.66.50.95", port=2000, **kwargs):
        self.address = address
        self.port = port
        self.ioLock = asyncio.Lock()
        super().__init__(*args, **kwargs)

    async def __ainit__(self, async_lib):
        # Turn on echo so that we know if device is communicating
        await self.write_and_read("W330", "00011")

    async def write_and_read(self, command, value=None):
        async with self.ioLock:
            reader, writer = await asyncio.open_connection(self.address, self.port)
            start_of_frame = "*"
            termination = "\r"
            if value is None:
                msg = f"{start_of_frame}{command}{termination}"
            else:
                msg = f"{start_of_frame}{command} {value}{termination}"
            print(msg)
            writer.write(msg.encode())
            await writer.drain()
            data = await reader.read(100)
            response = data.decode().rstrip()[len(command):]
            writer.close()
            await writer.wait_closed()
        return response

    @sensor.startup
    async def sensor(self, instance, async_lib):
        print("running sensor startup hook")
        sensor_config = await self.write_and_read("R100")
        print(sensor_config)
        stype, si1, si2 = self.parse_sensor_config(sensor_config)
        await instance.write(stype)
        await self.si1.write(si1)
        await self.si2.write(si2)

    def parse_sensor_config(self, sensor_config):
        stype = int(sensor_config[0])
        si1i = int(sensor_config[1])
        si2i = int(sensor_config[2])
        si1 = si1_lookup.get(stype, {}).get(si1i, "")
        si2 = si2_lookup.get(stype, {}).get(si2i, "")
        return stype, si1, si2
        
    @output_chan.startup
    async def output_chan(self, instance, async_lib):
        await self.update_output_config()
        
    async def update_output_config(self, chan=None):
        if chan is None:
            chan = self.output_chan.value
        mode = int(await self.write_and_read("R600", chan))
        await self.output_mode.write(mode)
        outtype = await self.write_and_read("G601", chan)
        await self.output_type.write(output_type_lookup.get(outtype, "No Output"))
        outrange = int(await self.write_and_read("R660", chan))
        await self.output_range.write(outrange)

    @output_chan.putter
    async def output_chan(self, instance, value):
        await self.update_output_config(value)

    @output_mode.putter
    async def output_mode(self, instance, value):
        chan = self.output_chan.value
        rawval = instance.get_raw_value(value)
        cmd = f"{chan}{rawval}"
        await self.write_and_read("W600", cmd)

    @output_range.putter
    async def output_range(self, instance, value):
        chan = self.output_chan.value
        rawval = instance.get_raw_value(value)
        cmd = f"{chan}{rawval}"
        await self.write_and_read("W660", cmd)
        
    @setpoint.startup
    async def setpoint(self, instance, async_lib):
        sp = await self.write_and_read("R400")
        sp = float(sp.lstrip("+"))
        await instance.write(sp)

    @setpoint.putter
    async def setpoint(self, instance, value):
        await self.write_and_read("W400", value)

    @pid_hi_lim.putter
    async def pid_hi_lim(self, instance, value):
        hex_val = f"{value:02X}"
        await self.write_and_read("W502", hex_val)
    
    @temperature.scan(period=2, use_scan_field=True)
    async def temperature(self, instance, async_lib):
        r = await self.write_and_read("G110")
        t = float(r.lstrip("+"))
        await instance.write(t)

    
    
if __name__ == "__main__":
    ioc_options, run_options = ioc_arg_parser(default_prefix="cn8pt:",
                                              desc = dedent(CN8PT.__doc__),
                                              supported_async_libs=('asyncio',))
    ioc = CN8PT(**ioc_options)
    run(ioc.pvdb, startup_hook=ioc.__ainit__, **run_options)
