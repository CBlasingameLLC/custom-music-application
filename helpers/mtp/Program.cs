// mtk-mtp: talks to phones and players that have no drive letter (MTP) on behalf of Music Toolkit.
//
// Music Toolkit starts this program and speaks to it in JSON lines: one request per line on standard input, one reply
// per line on standard output. The first line it prints is {"event":"ready","version":1}. The commands and what they
// answer are described in src/musictoolkit/sync/mtp.py. Anything this program has to say for a human goes to standard
// error; standard output carries nothing but replies.
//
//   mtk-mtp.exe --selftest     prints {"ok":true,...} with the number of devices Windows lists, and exits.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;
using MediaDevices;

namespace MusicToolkit.Mtp
{
    internal static class Program
    {
        internal const int ProtocolVersion = 1;

        private static int Main(string[] args)
        {
            try
            {
                if (args.Length > 0 && args[0] == "--selftest")
                {
                    return SelfTest();
                }

                var utf8 = new UTF8Encoding(false); // no byte order mark: the other side reads plain UTF-8 lines
                var input = new StreamReader(Console.OpenStandardInput(), utf8);
                var output = new StreamWriter(Console.OpenStandardOutput(), utf8) { AutoFlush = true, NewLine = "\n" };
                return new Server(input, output).Run();
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine("mtk-mtp stopped: " + exception);
                return 1;
            }
        }

        /// <summary>Proves the program starts and Windows Portable Devices answers, without opening any device.</summary>
        private static int SelfTest()
        {
            var serializer = new JavaScriptSerializer();
            int count = MediaDevice.GetDevices().Count();
            Console.Out.WriteLine(serializer.Serialize(new Dictionary<string, object>
            {
                { "ok", true },
                { "helper", "mtk-mtp" },
                { "version", ProtocolVersion },
                { "devices", count },
            }));
            return 0;
        }
    }
}
