// mtk-disc: reads audio CDs, ejects discs, burns discs and makes disc images on behalf of Music Toolkit.
//
// Music Toolkit starts this program and speaks to it in JSON lines: one request per line on standard input, one reply per
// line on standard output. The first line it prints is {"event":"ready","version":1}. The commands and what they answer are
// described in src/musictoolkit/discs/helper.py. A request that takes a while may print progress lines before its reply, and
// {"cmd":"cancel","target":3} asks the program to stop request 3. Anything this program has to say for a human goes to standard
// error; standard output carries nothing but replies.
//
//   mtk-disc.exe --selftest     prints {"ok":true,...} with the number of optical drives Windows lists, and exits.

using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;

namespace MusicToolkit.Disc
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
                Console.Error.WriteLine("mtk-disc stopped: " + exception);
                return 1;
            }
        }

        /// <summary>Proves the program starts, that Windows answers its calls and that the structure a raw read is asked with is the size
        /// Windows expects (a wrong size would make every read fail, or worse, misread), without opening any drive.</summary>
        private static int SelfTest()
        {
            var serializer = new JavaScriptSerializer();
            int requestSize = Marshal.SizeOf(typeof(Native.RawReadInfo));
            bool onWindows = Environment.OSVersion.Platform == PlatformID.Win32NT;
            int optical = Drives.Optical().Count;
            bool kernel = true;
            if (onWindows)
            {
                kernel = Native.GetDriveType("C:\\") != 0; // 0 means the call itself failed
            }

            bool ok = requestSize == 16 && kernel;
            Console.Out.WriteLine(serializer.Serialize(new Dictionary<string, object>
            {
                { "ok", ok },
                { "helper", "mtk-disc" },
                { "version", ProtocolVersion },
                { "drives", optical },
                { "request_size", requestSize },
            }));
            return ok ? 0 : 1;
        }
    }
}
