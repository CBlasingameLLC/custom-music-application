using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Web.Script.Serialization;

namespace MusicToolkit.Disc
{
    /// <summary>Reads requests from Music Toolkit one line at a time, does them one after another, and answers each in one line.
    /// A request to stop ({"cmd":"cancel","target":3}) is read at once, even while the request it is about is still being worked on.</summary>
    internal sealed class Server
    {
        /// <summary>The request being worked on: lets a command say how far it is and notice that it was asked to stop.</summary>
        internal sealed class Call
        {
            private readonly Server server;
            public readonly object Id;
            public readonly long Number;

            public Call(Server server, object id, long number)
            {
                this.server = server;
                Id = id;
                Number = number;
            }

            /// <summary>True once the person has asked to stop this request.</summary>
            public bool Cancelled
            {
                get { return server.WasCancelled(Number); }
            }

            /// <summary>Throws a refusal with the code "cancelled" if the person asked to stop this request.</summary>
            public void Check()
            {
                if (server.WasCancelled(Number))
                {
                    throw new DiscFailure(Failures.Cancelled, "Stopped.");
                }
            }

            public void Progress(long done, long total, string message)
            {
                server.Send(new Dictionary<string, object>
                {
                    { "event", "progress" }, { "id", Id }, { "done", done }, { "total", total }, { "message", message },
                });
            }
        }

        private readonly TextReader input;
        private readonly TextWriter output;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
        private readonly object writing = new object();
        private readonly object gate = new object();
        private readonly HashSet<long> cancelled = new HashSet<long>();
        private readonly BlockingCollection<Dictionary<string, object>> requests = new BlockingCollection<Dictionary<string, object>>();
        private readonly Dictionary<string, Drive> open = new Dictionary<string, Drive>(StringComparer.OrdinalIgnoreCase);
        private readonly bool testing = Environment.GetEnvironmentVariable("MTK_DISC_TEST") == "1"; // switches on the commands the checks use

        public Server(TextReader input, TextWriter output)
        {
            this.input = input;
            this.output = output;
        }

        public int Run()
        {
            Send(new Dictionary<string, object> { { "event", "ready" }, { "version", Program.ProtocolVersion } });
            var reader = new Thread(ReadRequests) { IsBackground = true, Name = "requests" };
            reader.Start();
            foreach (Dictionary<string, object> request in requests.GetConsumingEnumerable())
            {
                object id;
                request.TryGetValue("id", out id);
                string command = Text(request, "cmd");
                if (command == "close")
                {
                    Send(new Dictionary<string, object> { { "id", id }, { "ok", true } });
                    break;
                }

                long number = Whole(id, -1);
                Dictionary<string, object> reply;
                try
                {
                    reply = Handle(command, request, new Call(this, id, number));
                }
                catch (Exception exception)
                {
                    if (!(exception is DiscFailure))
                    {
                        Console.Error.WriteLine(command + " failed: " + exception);
                    }

                    reply = Refusal(Failures.Of(exception));
                }

                reply["id"] = id;
                Send(reply);
                lock (gate)
                {
                    cancelled.RemoveWhere(target => target <= number); // a request that has been answered can no longer be stopped
                }
            }

            foreach (Drive drive in open.Values)
            {
                drive.Dispose();
            }

            open.Clear();
            return 0;
        }

        /// <summary>Its own thread, so that "stop" is heard while the worker is busy with a long request.</summary>
        private void ReadRequests()
        {
            try
            {
                string line;
                while ((line = input.ReadLine()) != null)
                {
                    if (line.Trim().Length == 0)
                    {
                        continue;
                    }

                    Dictionary<string, object> request;
                    try
                    {
                        request = json.DeserializeObject(line) as Dictionary<string, object>;
                    }
                    catch (Exception)
                    {
                        continue; // not a request
                    }

                    if (request == null)
                    {
                        continue;
                    }

                    if (Text(request, "cmd") == "cancel")
                    {
                        object target;
                        request.TryGetValue("target", out target);
                        lock (gate)
                        {
                            cancelled.Add(Whole(target, -2));
                        }

                        continue;
                    }

                    requests.Add(request);
                }
            }
            catch (Exception)
            {
                // the input is gone: finish what is being done, then stop
            }
            finally
            {
                requests.CompleteAdding();
            }
        }

        private bool WasCancelled(long number)
        {
            lock (gate)
            {
                return cancelled.Contains(number);
            }
        }

        private void Send(Dictionary<string, object> message)
        {
            string line = json.Serialize(message);
            lock (writing)
            {
                output.WriteLine(line);
            }
        }

        // ------------------------------------------------------------------------------------- commands

        private Dictionary<string, object> Handle(string command, Dictionary<string, object> request, Call call)
        {
            switch (command)
            {
                case "drives":
                    return Ok(new Dictionary<string, object> { { "drives", ListDrives() } });
                case "toc":
                    return TableOfContents(Text(request, "drive"), call);
                case "read":
                    return Read(Text(request, "drive"), Whole(Get(request, "lba"), -1), (int)Whole(Get(request, "count"), 0), call);
                case "flush":
                    return Flush(Text(request, "drive"), Whole(Get(request, "lba"), -1), (int)Whole(Get(request, "count"), 0), call);
                case "eject":
                    return Eject(Text(request, "drive"));
                case "media":
                    return Ok(Imapi.Media(Text(request, "drive")));
                case "burn_audio":
                    return Ok(Imapi.BurnAudio(
                        Text(request, "drive"), Tracks(request), (int)Whole(Get(request, "speed"), 0), Get(request, "gapless") is bool && (bool)Get(request, "gapless"), call));
                case "burn_data":
                    return Ok(Imapi.BurnData(
                        Text(request, "drive"), Text(request, "label"), Text(request, "label_iso"), Files(request), Strings(Get(request, "filesystems")),
                        (int)Whole(Get(request, "speed"), 0), call));
                case "build_iso":
                    return Ok(Imapi.BuildIso(
                        Text(request, "label"), Text(request, "label_iso"), Files(request), Strings(Get(request, "filesystems")), Text(request, "out"), call));
                case "inspect":
                    return Ok(new Dictionary<string, object> { { "songs", Decoder.Inspect(Strings(Get(request, "paths"))) } });
                case "decode":
                    return Ok(Decoder.Decode(Text(request, "src"), Text(request, "out"), call));
                case "erase":
                    return Ok(Imapi.Erase(Text(request, "drive"), Get(request, "full") is bool && (bool)Get(request, "full"), call));
                case "imapi_probe" when testing:
                    return Ok(Imapi.Probe());
                case "raw_image" when testing:
                    return Ok(Imapi.RawProbe(
                        Tracks(request), (int)Whole(Get(request, "type"), 1), Get(request, "property") is bool && (bool)Get(request, "property"), Text(request, "out"), call));
                case "ticks" when testing:
                    return Ticks((int)Whole(Get(request, "n"), 3), call);
                case "wait" when testing:
                    return Wait((int)Whole(Get(request, "ms"), 1000), call);
                case "crash" when testing:
                    Environment.Exit(3);
                    return null;
                default:
                    return Refusal(new DiscFailure(Failures.Failed, "unknown command " + command));
            }
        }

        private List<object> ListDrives()
        {
            var found = new List<object>();
            foreach (string id in Drives.Optical())
            {
                string media = "unknown";
                string label = string.Empty;
                try
                {
                    media = Look(id, out label);
                }
                catch (Exception exception)
                {
                    Console.Error.WriteLine("Looking at " + id + " failed: " + exception.Message);
                }

                var entry = new Dictionary<string, object>
                {
                    { "id", id }, { "name", Drives.HardwareName(id) ?? ("CD/DVD drive (" + id + ")") }, { "media", media }, { "label", label },
                };
                foreach (KeyValuePair<string, object> fact in Imapi.Describe(id))
                {
                    entry[fact.Key] = fact.Value; // whether it writes, and which kinds of disc
                }

                found.Add(entry);
            }

            return found;
        }

        /// <summary>What is in a drive, in a word: none, loading, audio, data, mixed (music and a data track, as on a CD-Extra) or unknown (a
        /// blank disc, or one the drive cannot read).</summary>
        private static string Look(string id, out string label)
        {
            label = string.Empty;
            using (Drive drive = Drive.Open(id))
            {
                try
                {
                    drive.CheckMedia(0, null);
                }
                catch (DiscFailure failure)
                {
                    return failure.Code == Failures.NoMedia ? "none" : failure.Code == Failures.NotReady ? "loading" : "unknown";
                }

                try
                {
                    var info = new DriveInfo(id);
                    if (info.IsReady)
                    {
                        label = info.VolumeLabel ?? string.Empty;
                    }
                }
                catch (Exception)
                {
                    // a label is a courtesy
                }

                Contents contents;
                try
                {
                    contents = drive.ReadContents(0, null);
                }
                catch (DiscFailure failure)
                {
                    return failure.Code == Failures.NotReady ? "loading" : "unknown";
                }

                int audio = contents.Tracks.FindAll(t => t.Audio).Count;
                return audio == 0 ? "data" : audio == contents.Tracks.Count ? "audio" : "mixed";
            }
        }

        /// <summary>The drive for this request, opened if it is not already (a rip reads one drive through many requests).</summary>
        private Drive Held(string id)
        {
            string letter = Drives.Require(id);
            Drive drive;
            if (!open.TryGetValue(letter, out drive))
            {
                drive = Drive.Open(letter);
                open[letter] = drive;
            }

            return drive;
        }

        private void Release(string id)
        {
            string letter = Drives.Normalize(id);
            Drive drive;
            if (open.TryGetValue(letter, out drive))
            {
                drive.Dispose();
                open.Remove(letter);
            }
        }

        private Dictionary<string, object> TableOfContents(string id, Call call)
        {
            string letter = Drives.Require(id);
            Release(letter); // a fresh look: whatever the old handle remembered about the disc is not to be trusted
            Drive drive = Held(letter);
            try
            {
                drive.CheckMedia(40, call.Check);
                Contents contents = drive.ReadContents(40, call.Check);
                var tracks = new List<object>();
                foreach (Track track in contents.Tracks)
                {
                    tracks.Add(new Dictionary<string, object> { { "number", track.Number }, { "audio", track.Audio }, { "start", track.Start } });
                }

                return Ok(new Dictionary<string, object>
                {
                    { "first", contents.First }, { "last", contents.Last }, { "leadout", contents.LeadOut }, { "tracks", tracks },
                });
            }
            catch (Exception)
            {
                Release(letter);
                throw;
            }
        }

        private Dictionary<string, object> Read(string id, long lba, int count, Call call)
        {
            if (lba < 0 || count < 1 || count > Drive.MaxRead)
            {
                return Refusal(new DiscFailure(Failures.Failed, "Bad sector range."));
            }

            byte[] data = Held(id).ReadAudio(lba, count, call.Check);
            return Ok(new Dictionary<string, object> { { "sectors", count }, { "data", Convert.ToBase64String(data) } });
        }

        private Dictionary<string, object> Flush(string id, long lba, int count, Call call)
        {
            if (lba < 0 || count < 1 || count > 4096)
            {
                return Refusal(new DiscFailure(Failures.Failed, "Bad sector range."));
            }

            Held(id).Flush(lba, count, call.Check);
            return Ok();
        }

        private Dictionary<string, object> Eject(string id)
        {
            string letter = Drives.Require(id);
            Release(letter);
            using (Drive drive = Drive.Open(letter))
            {
                drive.Eject();
            }

            return Ok();
        }

        // ------------------------------------------------------------------------------------- reading what a request lists

        private static IEnumerable<Dictionary<string, object>> Objects(object list)
        {
            var items = list as System.Collections.IEnumerable;
            if (items == null || list is string)
            {
                yield break;
            }

            foreach (object item in items)
            {
                var one = item as Dictionary<string, object>;
                if (one != null)
                {
                    yield return one;
                }
            }
        }

        private static List<string> Strings(object list)
        {
            var found = new List<string>();
            var items = list as System.Collections.IEnumerable;
            if (items != null && !(list is string))
            {
                foreach (object item in items)
                {
                    var text = item as string;
                    if (text != null)
                    {
                        found.Add(text);
                    }
                }
            }

            return found;
        }

        private static List<Imapi.TrackFile> Tracks(Dictionary<string, object> request)
        {
            return Objects(Get(request, "tracks")).Select(t => new Imapi.TrackFile { Path = Text(t, "path"), Sectors = Whole(Get(t, "sectors"), 0) }).ToList();
        }

        private static List<Imapi.FileItem> Files(Dictionary<string, object> request)
        {
            return Objects(Get(request, "files")).Select(f => new Imapi.FileItem { Source = Text(f, "src"), Dest = Text(f, "dest") }).ToList();
        }

        // ------------------------------------------------------------------------------------- for the checks (MTK_DISC_TEST=1)

        /// <summary>n progress events a few milliseconds apart, so that a check can stop the request half way.</summary>
        private Dictionary<string, object> Ticks(int n, Call call)
        {
            for (int i = 1; i <= n; i++)
            {
                call.Check();
                call.Progress(i, n, "step " + i);
                Thread.Sleep(20);
            }

            return Ok();
        }

        private Dictionary<string, object> Wait(int milliseconds, Call call)
        {
            for (int waited = 0; waited < milliseconds; waited += 10)
            {
                call.Check();
                Thread.Sleep(10);
            }

            return Ok();
        }

        // ------------------------------------------------------------------------------------- small helpers

        private static Dictionary<string, object> Ok(Dictionary<string, object> more = null)
        {
            var reply = more ?? new Dictionary<string, object>();
            reply["ok"] = true;
            return reply;
        }

        private static Dictionary<string, object> Refusal(DiscFailure failure)
        {
            return new Dictionary<string, object> { { "ok", false }, { "code", failure.Code }, { "error", failure.Message } };
        }

        private static object Get(Dictionary<string, object> request, string key)
        {
            object value;
            return request.TryGetValue(key, out value) ? value : null;
        }

        private static string Text(Dictionary<string, object> request, string key)
        {
            return Get(request, key) as string ?? string.Empty;
        }

        private static long Whole(object value, long otherwise)
        {
            try
            {
                return value == null ? otherwise : Convert.ToInt64(value);
            }
            catch (Exception)
            {
                return otherwise;
            }
        }
    }
}
