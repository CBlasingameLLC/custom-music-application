using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using ComStream = System.Runtime.InteropServices.ComTypes.IStream;

namespace MusicToolkit.Disc
{
    /// <summary>Burning, erasing and making disc images, with Windows' own disc mastering component (IMAPI 2).
    ///
    /// IMAPI is reached late-bound: objects are made from their program ids ("IMAPI2.MsftDiscFormat2Data") and called by name, so nothing has to be
    /// compiled against its type library and a wrong guess about a method shows up as an error from that one call, never as a crash. Everything
    /// that is handed to IMAPI as a stream is a stream the system makes over a file (SHCreateStreamOnFileEx), so no object of ours has to cross
    /// into IMAPI as a COM object. Progress comes from IMAPI's own events where they can be connected, and from a once-a-second report either way,
    /// so a long burn is never silent.</summary>
    internal static class Imapi
    {
        public const string Client = "Music Toolkit";
        private const int SectorBytes = 2048;
        private const int AudioSectorBytes = 2352;
        private const int AudioGapSectors = 150;
        private const int MinimumAudioSectors = 300;
        private const int MaxAudioTracks = 99;

        // IMAPI_CD_SECTOR_TYPE and IMAPI_FORMAT2_RAW_CD_DATA_SECTOR_TYPE (what the sub-channel part of the image holds)
        private const int CdSectorAudio = 0;
        private const int RawPqOnly = 1;
        private const int RawCooked = 2;
        private const int RawRaw = 3;
        private const string RawCreatorProgId = "IMAPI2.MsftRawCDImageCreator";
        private const string RawCreatorClassId = "25983561-9D65-49CE-B335-40630D901227";

        // IMAPI_MEDIA_PHYSICAL_TYPE
        private static readonly Dictionary<int, string> Kinds = new Dictionary<int, string>
        {
            { 1, "CD-ROM" }, { 2, "CD-R" }, { 3, "CD-RW" }, { 4, "DVD-ROM" }, { 5, "DVD-RAM" }, { 6, "DVD+R" }, { 7, "DVD+RW" },
            { 8, "DVD+R DL" }, { 9, "DVD-R" }, { 10, "DVD-RW" }, { 11, "DVD-R DL" }, { 12, "disk" }, { 13, "DVD+RW DL" },
            { 14, "HD DVD-ROM" }, { 15, "HD DVD-R" }, { 16, "HD DVD-RAM" }, { 17, "BD-ROM" }, { 18, "BD-R" }, { 19, "BD-RE" },
        };

        private static readonly HashSet<int> RewritableKinds = new HashSet<int> { 3, 5, 7, 10, 13, 19 };

        // IMAPI_PROFILE_TYPE (the MMC profile numbers) of what a recorder can write, and what each is called
        private static readonly Dictionary<int, string> WritableProfiles = new Dictionary<int, string>
        {
            { 0x09, "CD-R" }, { 0x0A, "CD-RW" }, { 0x11, "DVD-R" }, { 0x13, "DVD-RW" }, { 0x14, "DVD-RW" }, { 0x15, "DVD-R DL" }, { 0x16, "DVD-R DL" },
            { 0x1A, "DVD+RW" }, { 0x1B, "DVD+R" }, { 0x2A, "DVD+RW DL" }, { 0x2B, "DVD+R DL" }, { 0x41, "BD-R" }, { 0x42, "BD-R" }, { 0x43, "BD-RE" },
        };

        private static readonly string[] KindOrder =
        {
            "CD-R", "CD-RW", "DVD-R", "DVD-RW", "DVD+R", "DVD+RW", "DVD-R DL", "DVD+R DL", "DVD+RW DL", "BD-R", "BD-RE",
        };

        // IMAPI_FORMAT2_DATA_MEDIA_STATE (the track-at-once state has the same bits)
        private const int StateBlank = 0x2;
        private const int StateDamaged = 0x400;
        private const int StateWriteProtected = 0x2000;
        private const int StateFinalized = 0x4000;
        private const int StateUnsupportedMedia = 0x8000;
        private const int StateUnsupportedMask = 0xFC00;

        // FsiFileSystems
        private const int FsIso9660 = 1;
        private const int FsJoliet = 2;
        private const int FsUdf = 4;

        /// <summary>One recorder as IMAPI knows it.</summary>
        internal sealed class Recorder
        {
            public object Com;                                   // the IDiscRecorder2
            public string Id;                                    // IMAPI's own id for it
            public string Letter;                                // "E:"
            public string Product = string.Empty;
            public int[] Profiles = new int[0];
            public readonly List<string> Writes = new List<string>();

            public bool Writer
            {
                get { return Writes.Count > 0; }
            }
        }

        internal sealed class TrackFile
        {
            public string Path;
            public long Sectors;
        }

        internal sealed class FileItem
        {
            public string Source;
            public string Dest;
        }

        // ------------------------------------------------------------------------------------- failures

        /// <summary>What IMAPI's error numbers mean for the person, and for the program: the kinds are the ones in src/musictoolkit/discs/helper.py.</summary>
        internal static DiscFailure Fail(COMException error)
        {
            uint code = unchecked((uint)error.ErrorCode);
            switch (code)
            {
                case 0xC0AA0002u: // E_IMAPI_REQUEST_CANCELLED
                    return new DiscFailure(Failures.Cancelled, "Stopped.");
                case 0xC0AA0202u: // E_IMAPI_RECORDER_MEDIA_NO_MEDIA
                    return new DiscFailure(Failures.NoMedia, "There is no disc in the drive.");
                case 0xC0AA0203u: // E_IMAPI_RECORDER_MEDIA_INCOMPATIBLE
                case 0xC0AA0406u: // E_IMAPI_DF2DATA_MEDIA_IS_NOT_SUPPORTED
                case 0xC0AA0507u: // E_IMAPI_DF2TAO_MEDIA_IS_NOT_SUPPORTED
                    return new DiscFailure(Failures.Unsupported, "This drive cannot write to this kind of disc.");
                case 0xC0AA060Eu: // E_IMAPI_DF2RAW_DATA_BLOCK_TYPE_NOT_SUPPORTED
                case 0xC0AA060Fu: // E_IMAPI_DF2RAW_STREAM_LEADIN_TOO_SHORT
                    return new DiscFailure(Failures.Unsupported, NoGapsExplained);
                case 0xC0AA0204u: // E_IMAPI_RECORDER_MEDIA_UPSIDE_DOWN
                    return new DiscFailure(Failures.Unsupported, "The disc is in upside down.");
                case 0xC0AA0205u: // E_IMAPI_RECORDER_MEDIA_BECOMING_READY
                    return new DiscFailure(Failures.NotReady, "The drive is not ready yet. Wait until the disc has stopped spinning up and try again.");
                case 0xC0AA0206u: // E_IMAPI_RECORDER_MEDIA_FORMAT_IN_PROGRESS
                case 0xC0AA0207u: // E_IMAPI_RECORDER_MEDIA_BUSY
                case 0xC0AA0210u: // E_IMAPI_RECORDER_LOCKED
                case 0xC0AA0500u: // E_IMAPI_DF2TAO_WRITE_IN_PROGRESS
                case 0xC0AA0600u: // E_IMAPI_DF2RAW_WRITE_IN_PROGRESS
                case 0xC0AA0400u: // E_IMAPI_DF2DATA_WRITE_IN_PROGRESS
                    return new DiscFailure(Failures.Busy, "The drive is busy with something else. Wait for it to finish, or close the other program that is using it.");
                case 0xC0AA0209u: // E_IMAPI_RECORDER_MEDIA_WRITE_PROTECTED
                    return new DiscFailure(Failures.WriteProtected, "This disc cannot be written to: it is write-protected or already closed. Use another disc.");
                case 0xC0AA020Du: // E_IMAPI_RECORDER_COMMAND_TIMEOUT
                    return new DiscFailure(Failures.Failed, "The drive stopped answering while it was being written to.");
                case 0xC0AA020Fu: // E_IMAPI_RECORDER_MEDIA_SPEED_MISMATCH
                    return new DiscFailure(Failures.Unsupported, "This disc cannot be written at the speed that was chosen. Choose Automatic.");
                case 0xC0AA0300u: // E_IMAPI_LOSS_OF_STREAMING
                    return new DiscFailure(Failures.Underrun, "The drive ran out of data while writing (a buffer underrun).");
                case 0xC0AA0506u: // E_IMAPI_DF2TAO_MEDIA_IS_NOT_BLANK
                    return new DiscFailure(Failures.NotBlank, "This disc is not blank.");
                case 0xC0AA0606u: // E_IMAPI_DF2RAW_MEDIA_IS_NOT_BLANK / _NOT_SUPPORTED (the documentation gives these two numbers the other way round on its two pages)
                case 0xC0AA0607u:
                    return new DiscFailure(Failures.NotBlank, "This disc cannot take a music CD: it is not blank, or it is not a kind of disc that only a blank CD-R or CD-RW is.");
                case 0xC0AA0508u: // E_IMAPI_DF2TAO_TRACK_LIMIT_REACHED
                    return new DiscFailure(Failures.Failed, "A music CD holds at most 99 songs.");
                case 0xC0AA0509u: // E_IMAPI_DF2TAO_NOT_ENOUGH_SPACE
                case 0xC0AA0609u: // E_IMAPI_DF2RAW_NOT_ENOUGH_SPACE
                case 0xC0AAB121u: // IMAPI_E_IMAGE_TOO_BIG
                    return new DiscFailure(Failures.NoSpace, "That does not fit on this disc.");
                case 0xC0AA0909u: // E_IMAPI_ERASE_MEDIA_IS_NOT_SUPPORTED
                    return new DiscFailure(Failures.Unsupported, "This kind of disc cannot be erased here.");
                case 0x80070005u: // E_ACCESSDENIED
                    return new DiscFailure(Failures.Busy, "Windows would not let Music Toolkit use the drive. Another program may have it to itself: close it and try again.");
            }

            string text = (error.Message ?? string.Empty).Trim();
            if (text.Length > 300)
            {
                text = text.Substring(0, 300);
            }

            return new DiscFailure(Failures.Failed, "Windows' disc burning component reported a problem: " + text.TrimEnd('.') + " (0x" + code.ToString("X8") + ").");
        }

        private static bool Is(COMException error, uint code)
        {
            return unchecked((uint)error.ErrorCode) == code;
        }

        // ------------------------------------------------------------------------------------- finding recorders

        private static object Create(string programId)
        {
            Type type;
            try
            {
                type = Type.GetTypeFromProgID(programId, false);
            }
            catch (Exception)
            {
                type = null;
            }

            if (type == null)
            {
                throw new DiscFailure(Failures.Unsupported, "Windows' disc burning component (IMAPI) is not available on this computer.");
            }

            return Activator.CreateInstance(type);
        }

        private static IEnumerable<object> Items(object array)
        {
            var list = array as IEnumerable;
            if (list != null && !(array is string))
            {
                foreach (object item in list)
                {
                    yield return item;
                }
            }
        }

        private static string Kind(int type)
        {
            string name;
            return Kinds.TryGetValue(type, out name) ? name : string.Empty;
        }

        /// <summary>The recorder behind a drive letter, or null if IMAPI does not list it. The letter must already be one of an optical drive.</summary>
        internal static Recorder Find(string letter)
        {
            dynamic master = Create("IMAPI2.MsftDiscMaster2");
            bool supported;
            try
            {
                supported = master.IsSupportedEnvironment;
            }
            catch (COMException)
            {
                supported = false;
            }

            if (!supported)
            {
                throw new DiscFailure(Failures.Unsupported, "Windows' disc burning service is not running on this computer, so discs cannot be written.");
            }

            int count = master.Count;
            for (int i = 0; i < count; i++)
            {
                string id = IdAt(master, i);
                Recorder recorder = Open(id);
                if (recorder != null && string.Equals(recorder.Letter, letter, StringComparison.OrdinalIgnoreCase))
                {
                    return recorder;
                }
            }

            return null;
        }

        private static Exception Inner(Exception error)
        {
            var wrapped = error as System.Reflection.TargetInvocationException;
            return wrapped != null && wrapped.InnerException != null ? wrapped.InnerException : error;
        }

        /// <summary>The id of the recorder at this place in the list. The list answers to "Item" (its default member); it is asked by that name
        /// first, then as its default member, then the way the language writes an indexer, and if none of them is taken the reasons are said.</summary>
        private static string IdAt(object master, int index)
        {
            var reasons = new List<string>();
            foreach (string name in new[] { "Item", "[DISPID=0]" })
            {
                try
                {
                    return Convert.ToString(master.GetType().InvokeMember(name, System.Reflection.BindingFlags.GetProperty, null, master, new object[] { index }));
                }
                catch (Exception error)
                {
                    reasons.Add(name + ": " + Inner(error).Message.Trim());
                }
            }

            try
            {
                dynamic list = master;
                return list[index];
            }
            catch (Exception error)
            {
                reasons.Add("indexer: " + Inner(error).Message.Trim());
            }

            throw new DiscFailure(Failures.Failed, "Windows' disc burning component would not name its recorders (" + string.Join("; ", reasons) + ").");
        }

        private static Recorder Open(string id)
        {
            try
            {
                dynamic com = Create("IMAPI2.MsftDiscRecorder2");
                com.InitializeDiscRecorder(id);
                var recorder = new Recorder { Com = com, Id = id };
                foreach (object path in Items((object)com.VolumePathNames))
                {
                    string text = Convert.ToString(path) ?? string.Empty;
                    if (text.Length >= 2 && text[1] == ':')
                    {
                        recorder.Letter = char.ToUpperInvariant(text[0]) + ":";
                        break;
                    }
                }

                try
                {
                    recorder.Product = ((string)com.VendorId + " " + (string)com.ProductId).Trim();
                }
                catch (Exception)
                {
                    // a name is a courtesy
                }

                try
                {
                    recorder.Profiles = Items((object)com.SupportedProfiles).Select(p => Convert.ToInt32(p)).ToArray();
                }
                catch (Exception)
                {
                    recorder.Profiles = new int[0];
                }

                var kinds = new HashSet<string>();
                foreach (int profile in recorder.Profiles)
                {
                    string kind;
                    if (WritableProfiles.TryGetValue(profile, out kind))
                    {
                        kinds.Add(kind);
                    }
                }

                recorder.Writes.AddRange(KindOrder.Where(kinds.Contains));
                return recorder;
            }
            catch (DiscFailure)
            {
                throw;
            }
            catch (Exception)
            {
                return null; // a recorder that will not say who it is cannot be used
            }
        }

        private static Recorder Require(string drive)
        {
            string letter = Drives.Require(drive);
            Recorder recorder = Find(letter);
            if (recorder == null)
            {
                throw new DiscFailure(Failures.Unsupported, "Windows' disc burning component does not list this drive, so nothing can be written to it.");
            }

            return recorder;
        }

        private static Recorder RequireWriter(string drive)
        {
            Recorder recorder = Require(drive);
            if (!recorder.Writer)
            {
                throw new DiscFailure(Failures.Unsupported, "This drive cannot write discs.");
            }

            return recorder;
        }

        /// <summary>What the list of drives says about a recorder: whether it writes, and which kinds of disc. Never throws: a drive IMAPI cannot
        /// describe is simply one that does not write.</summary>
        internal static Dictionary<string, object> Describe(string letter)
        {
            var facts = new Dictionary<string, object> { { "writer", false }, { "writes", new List<object>() } };
            try
            {
                Recorder recorder = Find(letter);
                if (recorder == null)
                {
                    facts["imapi"] = "not listed";
                    return facts;
                }

                facts["writer"] = recorder.Writer;
                facts["writes"] = recorder.Writes.Cast<object>().ToList();
                facts["product"] = recorder.Product;
                facts["profiles"] = recorder.Profiles.Select(p => (object)("0x" + p.ToString("X"))).ToList();
            }
            catch (Exception exception)
            {
                facts["imapi_error"] = Failures.Of(exception).Message;
            }

            return facts;
        }

        // ------------------------------------------------------------------------------------- the disc in a recorder

        private static int SpeedUnit(string kind)
        {
            // sectors a second at 1x: a CD is 75; a DVD 1385 kilobytes a second, which is 676 sectors; a Blu-ray 4.5 megabytes, which is 2197
            return kind.StartsWith("CD", StringComparison.Ordinal) ? 75 : kind.StartsWith("BD", StringComparison.Ordinal) ? 2197 : 676;
        }

        internal static Dictionary<string, object> Media(string drive)
        {
            Recorder recorder = Require(drive);
            string root = recorder.Letter + "\\";
            var reply = new Dictionary<string, object>
            {
                { "writer", recorder.Writer }, { "writes", recorder.Writes.Cast<object>().ToList() }, { "speeds", new List<object>() },
                { "state", "none" }, { "type", string.Empty }, { "rewritable", false }, { "blank", false }, { "protected", false },
                { "capacity_sectors", 0L }, { "free_sectors", 0L }, { "can_audio", false }, { "gapless", false },
            };
            if (!recorder.Writer)
            {
                bool ready;
                try
                {
                    ready = new DriveInfo(recorder.Letter).IsReady;
                }
                catch (Exception)
                {
                    ready = false;
                }

                reply["state"] = ready ? "written" : "none";
                if (ready)
                {
                    reply["root"] = root;
                }

                return reply;
            }

            dynamic data = Create("IMAPI2.MsftDiscFormat2Data");
            data.Recorder = recorder.Com;
            data.ClientName = Client;
            int status;
            int type;
            try
            {
                status = data.CurrentMediaStatus;
                type = (int)data.CurrentPhysicalMediaType;
            }
            catch (COMException error) when (Is(error, 0xC0AA0202u))
            {
                return reply; // no disc
            }
            catch (COMException error)
            {
                reply["state"] = "unusable";
                reply["detail"] = error.Message.Trim();
                return reply;
            }

            string kind = Kind(type);
            bool blank = (status & StateBlank) != 0 && (status & StateUnsupportedMask) == 0;
            bool pressed = kind.EndsWith("ROM", StringComparison.Ordinal); // a disc that is read-only: it has something on it, and that is all there is to say
            bool unusable = !pressed && ((status & (StateUnsupportedMedia | StateDamaged)) != 0 || kind.Length == 0);
            reply["type"] = kind;
            reply["rewritable"] = RewritableKinds.Contains(type);
            reply["protected"] = (status & (StateWriteProtected | StateFinalized)) != 0;
            reply["blank"] = blank;
            reply["state"] = blank ? "blank" : unusable ? "unusable" : "written";
            if (!blank)
            {
                reply["root"] = root;
            }

            try
            {
                reply["capacity_sectors"] = Convert.ToInt64(data.TotalSectorsOnMedia);
                reply["free_sectors"] = blank ? Convert.ToInt64(data.FreeSectorsOnMedia) : 0L;
            }
            catch (COMException)
            {
                // an unreadable disc has no size to report
            }

            try
            {
                int unit = SpeedUnit(kind);
                reply["speeds"] = Items((object)data.SupportedWriteSpeeds).Select(s => Convert.ToInt32(s)).Where(s => s > 0).Distinct().OrderBy(s => s)
                    .Select(s => (object)new Dictionary<string, object> { { "speed", s }, { "label", Math.Max(1, (int)Math.Round((double)s / unit)) + "x" } }).ToList();
            }
            catch (COMException)
            {
                // the recorder does not list speeds for this disc: it will choose
            }

            if (blank && (type == 2 || type == 3))
            {
                try
                {
                    dynamic tao = Create("IMAPI2.MsftDiscFormat2TrackAtOnce");
                    reply["can_audio"] = (bool)tao.IsRecorderSupported(recorder.Com) && (bool)tao.IsCurrentMediaSupported(recorder.Com);
                }
                catch (Exception)
                {
                    reply["can_audio"] = false;
                }

                try
                {
                    dynamic raw = Create("IMAPI2.MsftDiscFormat2RawCD");
                    raw.Recorder = recorder.Com;
                    List<int> types = OfferedSectorTypes((object)raw); // (null: the recorder will not say before the media is locked; then only the two questions decide)
                    reply["gapless"] = (bool)raw.IsRecorderSupported(recorder.Com) && (bool)raw.IsCurrentMediaSupported(recorder.Com)
                        && (types == null || types.Contains(RawPqOnly) || types.Contains(RawRaw) || types.Contains(RawCooked));
                }
                catch (Exception)
                {
                    reply["gapless"] = false;
                }
            }

            return reply;
        }

        // ------------------------------------------------------------------------------------- reporting and stopping while IMAPI works

        /// <summary>Says how far a long request is, at least once a second, from whatever the events last told it.</summary>
        private sealed class Reporter : IDisposable
        {
            private readonly Server.Call call;
            private readonly Thread thread;
            private readonly DateTime began = DateTime.UtcNow;
            private volatile bool stop;
            private long done;
            private long total;
            private string message;

            public Reporter(Server.Call call, long total, string message)
            {
                this.call = call;
                this.total = total;
                this.message = message;
                thread = new Thread(Run) { IsBackground = true, Name = "progress" };
                thread.Start();
            }

            public void Set(long newDone, long newTotal, string newMessage)
            {
                done = newDone;
                if (newTotal > 0)
                {
                    total = newTotal;
                }

                if (!string.IsNullOrEmpty(newMessage))
                {
                    message = newMessage;
                }
            }

            public void Say(string newMessage)
            {
                message = newMessage;
            }

            public int Seconds
            {
                get { return (int)(DateTime.UtcNow - began).TotalSeconds; }
            }

            private void Run()
            {
                while (!stop)
                {
                    try
                    {
                        call.Progress(Math.Min(done, total > 0 ? total : done), total, message);
                    }
                    catch (Exception)
                    {
                        return; // the output is gone
                    }

                    for (int i = 0; i < 10 && !stop; i++)
                    {
                        Thread.Sleep(100);
                    }
                }
            }

            public void Dispose()
            {
                stop = true;
                thread.Join(2000);
            }
        }

        /// <summary>Carries a press of Stop to IMAPI, which has no way of looking at a flag: it must be told, from another thread.</summary>
        private sealed class Watcher : IDisposable
        {
            private readonly Server.Call call;
            private readonly Action stopIt;
            private readonly Thread thread;
            private volatile bool stop;

            public Watcher(Server.Call call, Action stopIt)
            {
                this.call = call;
                this.stopIt = stopIt;
                thread = new Thread(Run) { IsBackground = true, Name = "stop" };
                thread.Start();
            }

            private void Run()
            {
                while (!stop)
                {
                    if (call.Cancelled)
                    {
                        try
                        {
                            stopIt();
                        }
                        catch (Exception)
                        {
                            // nothing is running just now; the next look tries again
                        }

                        for (int i = 0; i < 5 && !stop; i++)
                        {
                            Thread.Sleep(100);
                        }
                    }

                    Thread.Sleep(50);
                }
            }

            public void Dispose()
            {
                stop = true;
                thread.Join(2000);
            }
        }

        /// <summary>The receiver for IMAPI's progress events (a dispatch interface with one method, Update, number 0x200). Which interface the
        /// source sends them through is asked of the source itself, so no identifier has to be known here.</summary>
        [ComVisible(true)]
        [ClassInterface(ClassInterfaceType.AutoDispatch)]
        public sealed class Updates : ICustomQueryInterface
        {
            private readonly Action<object> handler;
            private readonly Guid events;

            public Updates(Action<object> handler, Guid events)
            {
                this.handler = handler;
                this.events = events;
            }

            [DispId(0x200)]
            public void Update([MarshalAs(UnmanagedType.IDispatch)] object sender, [MarshalAs(UnmanagedType.IDispatch)] object progress)
            {
                try
                {
                    handler(progress);
                }
                catch (Exception)
                {
                    // an event must never fail the burn it reports on
                }
            }

            public CustomQueryInterfaceResult GetInterface(ref Guid iid, out IntPtr ppv)
            {
                if (iid == events)
                {
                    ppv = Marshal.GetIDispatchForObject(this);
                    return CustomQueryInterfaceResult.Handled;
                }

                ppv = IntPtr.Zero;
                return CustomQueryInterfaceResult.NotHandled;
            }
        }

        private sealed class Subscription : IDisposable
        {
            private readonly IConnectionPoint point;
            private readonly int cookie;

            public Subscription(IConnectionPoint point, int cookie)
            {
                this.point = point;
                this.cookie = cookie;
            }

            public void Dispose()
            {
                try
                {
                    if (point != null)
                    {
                        point.Unadvise(cookie);
                    }
                }
                catch (Exception)
                {
                    // already gone
                }
            }
        }

        private sealed class Nothing : IDisposable
        {
            public void Dispose()
            {
            }
        }

        /// <summary>Connects `handler` to the progress events of an IMAPI object. If that cannot be done, the burn simply goes on without them.</summary>
        private static IDisposable Listen(object source, Action<object> handler)
        {
            try
            {
                var container = source as IConnectionPointContainer;
                if (container == null)
                {
                    return new Nothing();
                }

                IEnumConnectionPoints points;
                container.EnumConnectionPoints(out points);
                var one = new IConnectionPoint[1];
                if (points.Next(1, one, IntPtr.Zero) == 0 && one[0] != null)
                {
                    Guid events;
                    one[0].GetConnectionInterface(out events);
                    int cookie;
                    one[0].Advise(new Updates(handler, events), out cookie);
                    return new Subscription(one[0], cookie);
                }
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine("IMAPI progress events could not be connected: " + exception.Message);
            }

            return new Nothing();
        }

        private static ComStream OpenStream(string path)
        {
            try
            {
                ComStream stream;
                Native.SHCreateStreamOnFileEx(path, Native.StgmRead | Native.StgmShareDenyWrite, Native.FileAttributeNormal, false, IntPtr.Zero, out stream);
                return stream;
            }
            catch (COMException error)
            {
                throw new DiscFailure(Failures.Failed, Path.GetFileName(path) + " could not be opened (" + (error.Message ?? "error").Trim().TrimEnd('.') + ").");
            }
        }

        private static string LocalFile(string path, string what)
        {
            if (string.IsNullOrEmpty(path) || path.StartsWith(@"\\.\", StringComparison.Ordinal) || path.StartsWith(@"\\?\", StringComparison.Ordinal)
                || !Path.IsPathRooted(path) || !File.Exists(path))
            {
                throw new DiscFailure(Failures.Failed, (string.IsNullOrEmpty(path) ? what : Path.GetFileName(path)) + " is not there.");
            }

            return path;
        }

        // ------------------------------------------------------------------------------------- a music CD

        /// <summary>Writes a music CD. Song by song (track at once) the recorder leaves two seconds of silence between the songs; with `gapless` the
        /// whole disc is laid out first and written in one go (disc at once), and the songs run on from one another.</summary>
        internal static Dictionary<string, object> BurnAudio(string drive, IList<TrackFile> tracks, int speed, bool gapless, Server.Call call)
        {
            drive = Drives.Require(drive); // first of all: nothing but the letter of an optical drive leads anywhere
            if (tracks.Count < 1 || tracks.Count > MaxAudioTracks)
            {
                throw new DiscFailure(Failures.Failed, "A music CD holds 1 to 99 songs.");
            }

            long need = 0;
            long between = gapless ? 0 : AudioGapSectors;
            var offsets = new List<long>();
            foreach (TrackFile track in tracks)
            {
                string path = LocalFile(track.Path, "A song");
                long size = new FileInfo(path).Length;
                if (size == 0 || size % AudioSectorBytes != 0 || size / AudioSectorBytes != track.Sectors || track.Sectors < MinimumAudioSectors)
                {
                    throw new DiscFailure(Failures.Failed, Path.GetFileName(path) + " is not audio the drive can write.");
                }

                offsets.Add(need);
                need += track.Sectors + between;
            }

            need -= between;
            Recorder recorder = RequireWriter(drive);
            dynamic tao = Create("IMAPI2.MsftDiscFormat2TrackAtOnce");
            tao.Recorder = recorder.Com;
            tao.ClientName = Client;
            if (!(bool)tao.IsRecorderSupported(recorder.Com))
            {
                throw new DiscFailure(Failures.Unsupported, "This drive cannot write music CDs.");
            }

            bool mediaOk;
            try
            {
                mediaOk = tao.IsCurrentMediaSupported(recorder.Com);
            }
            catch (COMException error) when (Is(error, 0xC0AA0202u))
            {
                throw new DiscFailure(Failures.NoMedia, "There is no disc in the drive.");
            }

            if (!mediaOk)
            {
                throw new DiscFailure(Failures.Unsupported, "Music can only be written to a blank CD-R or CD-RW.");
            }

            int state = tao.CurrentMediaStatus;
            if ((state & StateWriteProtected) != 0)
            {
                throw new DiscFailure(Failures.WriteProtected, "This disc cannot be written to: it is write-protected or already closed. Use another disc.");
            }

            if ((state & StateBlank) == 0)
            {
                throw new DiscFailure(Failures.NotBlank, "This disc is not blank.");
            }

            long free = Convert.ToInt64(tao.FreeSectorsOnMedia);
            if (need > free)
            {
                throw new DiscFailure(Failures.NoSpace, "The songs need " + need + " sectors and the disc has " + free + ".");
            }

            call.Check();
            if (gapless)
            {
                return BurnSeamless(recorder, tracks, need, speed, call);
            }

            if (speed > 0)
            {
                tao.SetWriteSpeed(speed, false);
            }

            Native.SetThreadExecutionState(Native.EsContinuous | Native.EsSystemRequired);
            try
            {
                using (var reporter = new Reporter(call, need, "Preparing the disc"))
                using (Listen((object)tao, args =>
                {
                    dynamic progress = args;
                    int number = progress.CurrentTrackNumber;
                    long start = Convert.ToInt64(progress.StartLba);
                    long last = Convert.ToInt64(progress.LastWrittenLba);
                    long count = Convert.ToInt64(progress.SectorCount);
                    int action = progress.CurrentAction;
                    long inTrack = Math.Max(0, Math.Min(last - start, count));
                    long before = number >= 1 && number <= offsets.Count ? offsets[number - 1] : 0;
                    reporter.Set(before + inTrack, need, action == 3 ? "Finishing the disc" : action == 1 ? "Preparing the disc" : "Song " + number + " of " + tracks.Count);
                }))
                using (new Watcher(call, () => tao.CancelAddTrack()))
                {
                    tao.PrepareMedia();
                    bool stoppedEarly = true;
                    try
                    {
                        for (int i = 0; i < tracks.Count; i++)
                        {
                            call.Check();
                            reporter.Set(offsets[i], need, "Song " + (i + 1) + " of " + tracks.Count);
                            ComStream stream = OpenStream(tracks[i].Path);
                            try
                            {
                                tao.AddAudioTrack(stream);
                            }
                            finally
                            {
                                Marshal.ReleaseComObject(stream);
                            }
                        }

                        reporter.Set(need, need, "Finishing the disc");
                        stoppedEarly = false;
                    }
                    finally
                    {
                        try
                        {
                            tao.ReleaseMedia();
                        }
                        catch (Exception) when (stoppedEarly)
                        {
                            // the reason the writing stopped is what the person is told; that the session could not be closed after it is only a consequence
                        }
                    }
                }
            }
            finally
            {
                Native.SetThreadExecutionState(Native.EsContinuous);
            }

            return new Dictionary<string, object> { { "tracks", tracks.Count }, { "sectors", need } };
        }

        // ------------------------------------------------------------------------------------- a music CD without gaps (disc at once)

        private const string NoGapsExplained = "This drive cannot write a music CD without gaps between the songs. Choose two seconds between songs instead.";

        /// <summary>The songs laid out as the image of a whole disc, and what IMAPI says about the layout it made. It holds on to the objects the
        /// image is read from (and so to the song files) until it is disposed.</summary>
        internal sealed class RawImage : IDisposable
        {
            public object Creator;                  // the IRawCDImageCreator
            public object Stream;                   // the image of the disc (an IStream), once it has been made
            public int SectorType;
            public bool Property;                   // what DisableGaplessAudio was set to
            public long[] Starts = new long[0];     // the first sector of each song, as IMAPI laid them out
            public long[] Counts = new long[0];
            public long LeadOut;
            public long Length;                     // the sectors of the songs, added up
            public bool Seamless;                   // the songs follow one another with nothing between them

            public void Dispose()
            {
                foreach (object com in new[] { Stream, Creator })
                {
                    try
                    {
                        if (com != null && Marshal.IsComObject(com))
                        {
                            Marshal.FinalReleaseComObject(com);
                        }
                    }
                    catch (Exception)
                    {
                        // tidying up must never hide why the burn stopped
                    }
                }

                Stream = null;
                Creator = null;
            }
        }

        private static object CreateRawImageCreator()
        {
            try
            {
                return Create(RawCreatorProgId);
            }
            catch (DiscFailure)
            {
                Type type;
                try
                {
                    type = Type.GetTypeFromCLSID(new Guid(RawCreatorClassId), false);
                }
                catch (Exception)
                {
                    type = null;
                }

                if (type == null)
                {
                    throw;
                }

                return Activator.CreateInstance(type);
            }
        }

        /// <summary>Gives IMAPI the songs to lay out as one disc. `property` is what DisableGaplessAudio is set to. Nothing is written anywhere.</summary>
        private static RawImage LayOut(IList<TrackFile> tracks, int sectorType, bool property, bool makeImage)
        {
            var image = new RawImage { SectorType = sectorType, Property = property };
            try
            {
                dynamic creator = CreateRawImageCreator();
                image.Creator = creator;
                creator.ResultingImageType = sectorType;
                creator.DisableGaplessAudio = property;
                foreach (TrackFile track in tracks)
                {
                    ComStream stream = OpenStream(track.Path);
                    try
                    {
                        creator.AddTrack(CdSectorAudio, stream);
                    }
                    finally
                    {
                        Marshal.ReleaseComObject(stream); // (the image keeps its own hold on it until it has been written)
                    }

                    image.Length += track.Sectors;
                }

                image.Starts = new long[tracks.Count];
                image.Counts = new long[tracks.Count];
                for (int i = 0; i < tracks.Count; i++)
                {
                    object info = ((object)creator).GetType().InvokeMember("TrackInfo", System.Reflection.BindingFlags.GetProperty, null, (object)creator, new object[] { i + 1 });
                    dynamic one = info;
                    image.Starts[i] = Convert.ToInt64(one.StartingLba);
                    image.Counts[i] = Convert.ToInt64(one.SectorCount);
                }

                image.LeadOut = Convert.ToInt64(creator.StartOfLeadout);
                // With nothing between the songs the disc, from the first song to the lead-out, is as long as the songs added up; two seconds between
                // each pair would make it 150 sectors longer for every pair.
                image.Seamless = image.LeadOut - image.Starts[0] == image.Length;
                if (makeImage)
                {
                    image.Stream = creator.CreateResultImage();
                }

                return image;
            }
            catch (Exception)
            {
                image.Dispose();
                throw;
            }
        }

        /// <summary>The songs as one disc image in which they run on from one another. DisableGaplessAudio is left false, which is what makes them run
        /// on (its documentation says the opposite; the check on a build machine lays songs out both ways and finds the whole 150 sectors longer for
        /// every pair with true). The layout is looked at all the same, and if it is not seamless nothing is written.</summary>
        private static RawImage SeamlessImage(IList<TrackFile> tracks, int sectorType)
        {
            RawImage image = LayOut(tracks, sectorType, false, true);
            if (!image.Seamless)
            {
                string how = (image.LeadOut - image.Starts[0]) + " sectors for songs that add up to " + image.Length;
                image.Dispose();
                throw new DiscFailure(Failures.Unsupported, "Windows' disc burning component would not lay the songs out without gaps (" + how + "). Choose two seconds between songs instead.");
            }

            return image;
        }

        /// <summary>The data sector types the recorder lists for disc-at-once writing, or null if it will not say (yet).</summary>
        private static List<int> OfferedSectorTypes(object raw)
        {
            try
            {
                dynamic recorder = raw;
                return Items((object)recorder.SupportedSectorTypes).Select(t => Convert.ToInt32(t)).ToList();
            }
            catch (COMException)
            {
                return null;
            }
        }

        private static int ChooseSectorType(ICollection<int> offered)
        {
            foreach (int type in new[] { RawPqOnly, RawRaw, RawCooked })
            {
                if (offered.Contains(type))
                {
                    return type;
                }
            }

            throw new DiscFailure(Failures.Unsupported, NoGapsExplained);
        }

        /// <summary>Writes the songs as one disc, without gaps: the whole disc is laid out first (by IMAPI's raw image maker, from the same song files)
        /// and written in one go (disc at once). Everything that can be refused is refused before the disc is touched.</summary>
        private static Dictionary<string, object> BurnSeamless(Recorder recorder, IList<TrackFile> tracks, long need, int speed, Server.Call call)
        {
            dynamic raw = Create("IMAPI2.MsftDiscFormat2RawCD");
            raw.Recorder = recorder.Com;
            raw.ClientName = Client;
            bool supported;
            try
            {
                supported = (bool)raw.IsRecorderSupported(recorder.Com) && (bool)raw.IsCurrentMediaSupported(recorder.Com);
            }
            catch (COMException error) when (Is(error, 0xC0AA0202u))
            {
                throw new DiscFailure(Failures.NoMedia, "There is no disc in the drive.");
            }

            if (!supported)
            {
                throw new DiscFailure(Failures.Unsupported, NoGapsExplained);
            }

            call.Check();
            List<int> offered = OfferedSectorTypes((object)raw); // (before the media is locked; some recorders only say once it is)
            raw.PrepareMedia();
            bool stoppedEarly = true;
            try
            {
                int type = ChooseSectorType(offered ?? OfferedSectorTypes((object)raw) ?? new List<int>());
                raw.RequestedSectorType = type;
                using (RawImage image = SeamlessImage(tracks, type))
                {
                    call.Check(); // (a last chance to stop with the disc untouched)
                    if (speed > 0)
                    {
                        raw.SetWriteSpeed(speed, false);
                    }

                    Native.SetThreadExecutionState(Native.EsContinuous | Native.EsSystemRequired);
                    try
                    {
                        using (var reporter = new Reporter(call, need, "Preparing the disc"))
                        using (Listen((object)raw, args =>
                        {
                            dynamic progress = args;
                            long start = Convert.ToInt64(progress.StartLba);
                            long last = Convert.ToInt64(progress.LastWrittenLba);
                            long count = Convert.ToInt64(progress.SectorCount);
                            string words = "Writing the disc";
                            try
                            {
                                int action = progress.CurrentAction;
                                words = action == 3 ? "Finishing the disc" : action == 1 ? "Preparing the disc" : "Writing the disc";
                            }
                            catch (Exception)
                            {
                                // the words are a courtesy
                            }

                            reporter.Set(count > 0 ? need * Math.Max(0, Math.Min(last - start, count)) / count : 0, need, words);
                        }))
                        using (new Watcher(call, () => raw.CancelWrite()))
                        {
                            raw.WriteMedia(image.Stream);
                            reporter.Set(need, need, "Finishing the disc");
                        }
                    }
                    finally
                    {
                        Native.SetThreadExecutionState(Native.EsContinuous);
                    }
                }

                stoppedEarly = false;
            }
            finally
            {
                try
                {
                    raw.ReleaseMedia();
                }
                catch (Exception) when (stoppedEarly)
                {
                    // the reason the writing stopped is what the person is told; that the session could not be closed after it is only a consequence
                }
            }

            return new Dictionary<string, object> { { "tracks", tracks.Count }, { "sectors", need }, { "gapless", true } };
        }

        /// <summary>For the checks (MTK_DISC_TEST=1): lays songs out as the image of a disc the way a burn without gaps would, and says what IMAPI made
        /// of them. With `output` the image itself is written there (under a name that is not taken), so that a check can look inside it. Needs no
        /// drive and writes nothing to one.</summary>
        internal static Dictionary<string, object> RawProbe(IList<TrackFile> tracks, int sectorType, bool property, string output, Server.Call call)
        {
            foreach (TrackFile track in tracks)
            {
                LocalFile(track.Path, "A song");
            }

            if (!string.IsNullOrEmpty(output)
                && (!Path.IsPathRooted(output) || output.StartsWith(@"\\", StringComparison.Ordinal) || File.Exists(output) || !Directory.Exists(Path.GetDirectoryName(output) ?? string.Empty)))
            {
                throw new DiscFailure(Failures.Failed, "That is not a place the image can be written.");
            }

            using (RawImage image = LayOut(tracks, sectorType, property, true))
            {
                var facts = new Dictionary<string, object>
                {
                    { "type", sectorType }, { "property", property }, { "seamless", image.Seamless }, { "starts", image.Starts.Cast<object>().ToList() },
                    { "counts", image.Counts.Cast<object>().ToList() }, { "leadout", image.LeadOut }, { "length", image.Length },
                };
                ComStream stream = (ComStream)image.Stream;
                try
                {
                    System.Runtime.InteropServices.ComTypes.STATSTG stat;
                    stream.Stat(out stat, 1);
                    facts["bytes"] = stat.cbSize;
                }
                catch (Exception exception)
                {
                    facts["bytes_error"] = exception.Message.Trim();
                }

                if (!string.IsNullOrEmpty(output))
                {
                    var buffer = new byte[1 << 20];
                    IntPtr read = Marshal.AllocCoTaskMem(sizeof(int));
                    long written = 0;
                    try
                    {
                        using (var file = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 20))
                        {
                            while (true)
                            {
                                call.Check();
                                stream.Read(buffer, buffer.Length, read);
                                int n = Marshal.ReadInt32(read);
                                if (n <= 0)
                                {
                                    break;
                                }

                                file.Write(buffer, 0, n);
                                written += n;
                                call.Progress(written, 0, "Reading the image");
                            }
                        }
                    }
                    finally
                    {
                        Marshal.FreeCoTaskMem(read);
                    }

                    facts["written"] = written;
                    facts["out"] = output;
                }

                return facts;
            }
        }

        // ------------------------------------------------------------------------------------- an image of files

        private static int FileSystems(IEnumerable<string> names)
        {
            int flags = 0;
            foreach (string name in names)
            {
                flags |= name == "iso9660" ? FsIso9660 : name == "joliet" ? FsJoliet : name == "udf" ? FsUdf : 0;
            }

            return flags == 0 ? FsIso9660 | FsJoliet : flags;
        }

        /// <summary>The file system image for these files. With a recorder it is sized for the disc in it; without one there is no limit.</summary>
        private static dynamic BuildImage(string label, string labelIso, IList<FileItem> files, IEnumerable<string> systems, object recorder, Server.Call call)
        {
            dynamic image = Create("IMAPI2FS.MsftFileSystemImage");
            if (recorder != null)
            {
                image.ChooseImageDefaults(recorder);
            }
            else
            {
                image.FreeMediaBlocks = 0; // zero is no limit
            }

            int flags = FileSystems(systems);
            image.FileSystemsToCreate = flags;
            image.VolumeName = label;
            if ((flags & FsIso9660) != 0)
            {
                TrySet(() => image.VolumeNameISO9660 = labelIso);
                TrySet(() => image.ISO9660InterchangeLevel = 2); // names of up to 31 characters for the readers that ignore Joliet
            }

            if ((flags & FsJoliet) != 0)
            {
                TrySet(() => image.VolumeNameJoliet = label);
            }

            if ((flags & FsUdf) != 0)
            {
                TrySet(() => image.VolumeNameUDF = label);
            }

            dynamic root = image.Root;
            var folders = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase) { { string.Empty, root } };
            foreach (FileItem file in files)
            {
                call.Check();
                string source = LocalFile(file.Source, "A file");
                string[] parts = (file.Dest ?? string.Empty).Split('/');
                if (parts.Length == 0 || parts.Any(p => p.Length == 0 || p == "." || p == ".." || p.IndexOf(':') >= 0 || p.IndexOf('\\') >= 0))
                {
                    throw new DiscFailure(Failures.Failed, "'" + file.Dest + "' is not a place on a disc.");
                }

                object directory = root;
                string path = string.Empty;
                for (int i = 0; i < parts.Length - 1; i++)
                {
                    path = path.Length == 0 ? parts[i] : path + "/" + parts[i];
                    object child;
                    if (!folders.TryGetValue(path, out child))
                    {
                        dynamic made = image.CreateDirectoryItem(parts[i]);
                        ((dynamic)directory).Add(made);
                        child = made;
                        folders[path] = child;
                    }

                    directory = child;
                }

                ComStream stream = OpenStream(source);
                try
                {
                    ((dynamic)directory).AddFile(parts[parts.Length - 1], stream);
                }
                finally
                {
                    Marshal.ReleaseComObject(stream); // (the image keeps its own hold on it until it has been written)
                }
            }

            return image;
        }

        private static void TrySet(Action set)
        {
            try
            {
                set();
            }
            catch (Exception)
            {
                // a refinement the component did not take: the defaults will do
            }
        }

        internal static Dictionary<string, object> BurnData(
            string drive, string label, string labelIso, IList<FileItem> files, IEnumerable<string> systems, int speed, Server.Call call)
        {
            drive = Drives.Require(drive);
            if (files.Count < 1)
            {
                throw new DiscFailure(Failures.Failed, "There is nothing to write.");
            }

            Recorder recorder = RequireWriter(drive);
            dynamic data = Create("IMAPI2.MsftDiscFormat2Data");
            data.Recorder = recorder.Com;
            data.ClientName = Client;
            bool mediaOk;
            try
            {
                mediaOk = data.IsCurrentMediaSupported(recorder.Com);
            }
            catch (COMException error) when (Is(error, 0xC0AA0202u))
            {
                throw new DiscFailure(Failures.NoMedia, "There is no disc in the drive.");
            }

            int state = data.CurrentMediaStatus;
            if ((state & StateWriteProtected) != 0)
            {
                throw new DiscFailure(Failures.WriteProtected, "This disc cannot be written to: it is write-protected or already closed. Use another disc.");
            }

            if (!mediaOk || (state & StateUnsupportedMask) != 0)
            {
                throw new DiscFailure(Failures.Unsupported, "This drive cannot write to this disc.");
            }

            if ((state & StateBlank) == 0)
            {
                throw new DiscFailure(Failures.NotBlank, "This disc is not blank.");
            }

            dynamic image = BuildImage(label, labelIso, files, systems, recorder.Com, call);
            dynamic result = image.CreateResultImage(); // too big for the disc: refused here (IMAPI_E_IMAGE_TOO_BIG), before anything is written
            long blocks = Convert.ToInt64(result.TotalBlocks);
            call.Check();
            data.ForceMediaToBeClosed = true; // nothing more can be added to the disc afterwards: it is finished
            if (speed > 0)
            {
                data.SetWriteSpeed(speed, false);
            }

            Native.SetThreadExecutionState(Native.EsContinuous | Native.EsSystemRequired);
            try
            {
                using (var reporter = new Reporter(call, blocks, "Preparing the disc"))
                using (Listen((object)data, args =>
                {
                    dynamic progress = args;
                    long start = Convert.ToInt64(progress.StartLba);
                    long last = Convert.ToInt64(progress.LastWrittenLba);
                    long count = Convert.ToInt64(progress.SectorCount);
                    int action = progress.CurrentAction;
                    reporter.Set(Math.Max(0, Math.Min(last - start, count)), count, action == 4 ? "Writing the files" : action == 5 ? "Finishing the disc" : "Preparing the disc");
                }))
                using (new Watcher(call, () => data.CancelWrite()))
                {
                    data.Write(result.ImageStream);
                    reporter.Set(blocks, blocks, "Finishing the disc");
                }
            }
            finally
            {
                Native.SetThreadExecutionState(Native.EsContinuous);
            }

            return new Dictionary<string, object> { { "files", files.Count }, { "sectors", blocks } };
        }

        /// <summary>An ISO image file of these files. Needs no drive. The file is written under another name and renamed when it is whole, and
        /// never replaces a file that is there.</summary>
        internal static Dictionary<string, object> BuildIso(
            string label, string labelIso, IList<FileItem> files, IEnumerable<string> systems, string output, Server.Call call)
        {
            if (files.Count < 1)
            {
                throw new DiscFailure(Failures.Failed, "There is nothing to write.");
            }

            if (string.IsNullOrEmpty(output) || !output.EndsWith(".iso", StringComparison.OrdinalIgnoreCase) || !Path.IsPathRooted(output)
                || output.StartsWith(@"\\.\", StringComparison.Ordinal) || output.StartsWith(@"\\?\", StringComparison.Ordinal))
            {
                throw new DiscFailure(Failures.Failed, "That is not a place an image can be written.");
            }

            string folder = Path.GetDirectoryName(output);
            if (folder == null || !Directory.Exists(folder))
            {
                throw new DiscFailure(Failures.Failed, "That folder is not there.");
            }

            if (File.Exists(output))
            {
                throw new DiscFailure(Failures.Failed, "A file with that name is already there.");
            }

            string part = output + ".part";
            bool made = false;
            try
            {
                dynamic image = BuildImage(label, labelIso, files, systems, null, call);
                dynamic result = image.CreateResultImage();
                long total = Convert.ToInt64(result.TotalBlocks) * SectorBytes;
                ComStream stream = (ComStream)result.ImageStream;
                var buffer = new byte[1 << 20];
                IntPtr read = Marshal.AllocCoTaskMem(sizeof(int));
                try
                {
                    using (var file = new FileStream(part, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 20))
                    {
                        long done = 0;
                        while (true)
                        {
                            call.Check();
                            stream.Read(buffer, buffer.Length, read);
                            int n = Marshal.ReadInt32(read);
                            if (n <= 0)
                            {
                                break;
                            }

                            file.Write(buffer, 0, n);
                            done += n;
                            call.Progress(done / SectorBytes, total / SectorBytes, "Making the image");
                        }

                        file.Flush();
                    }
                }
                finally
                {
                    Marshal.FreeCoTaskMem(read);
                    try
                    {
                        Marshal.ReleaseComObject(stream);
                    }
                    catch (Exception)
                    {
                        // tidying up must never hide why the image stopped
                    }
                }

                File.Move(part, output); // (refuses to replace a file that appeared in the meantime)
                made = true;
                return new Dictionary<string, object> { { "files", files.Count }, { "bytes", new FileInfo(output).Length }, { "sectors", total / SectorBytes } };
            }
            finally
            {
                if (!made)
                {
                    try
                    {
                        File.Delete(part);
                    }
                    catch (Exception)
                    {
                        // a program that is still looking at it (a virus scanner) holds it for a moment: it is tidied up with the folder, and the
                        // reason the image was not made must not be replaced by this
                    }
                }
            }
        }

        // ------------------------------------------------------------------------------------- erasing

        internal static Dictionary<string, object> Erase(string drive, bool full, Server.Call call)
        {
            drive = Drives.Require(drive);
            Recorder recorder = RequireWriter(drive);
            dynamic erase = Create("IMAPI2.MsftDiscFormat2Erase");
            erase.Recorder = recorder.Com;
            erase.ClientName = Client;
            if (!(bool)erase.IsRecorderSupported(recorder.Com))
            {
                throw new DiscFailure(Failures.Unsupported, "This drive cannot erase discs.");
            }

            bool mediaOk;
            try
            {
                mediaOk = erase.IsCurrentMediaSupported(recorder.Com);
            }
            catch (COMException error) when (Is(error, 0xC0AA0202u))
            {
                throw new DiscFailure(Failures.NoMedia, "There is no disc in the drive.");
            }

            if (!mediaOk)
            {
                throw new DiscFailure(Failures.Unsupported, "This disc cannot be erased here. Only a CD-RW or a DVD-RW can be (a DVD+RW, DVD-RAM or BD-RE is overwritten, not erased).");
            }

            erase.FullErase = full;
            call.Check();
            Native.SetThreadExecutionState(Native.EsContinuous | Native.EsSystemRequired);
            try
            {
                using (var reporter = new ErasingReporter(call))
                {
                    erase.EraseMedia(); // takes as long as it takes; IMAPI gives no way of stopping it
                    reporter.Finish();
                }
            }
            finally
            {
                Native.SetThreadExecutionState(Native.EsContinuous);
            }

            return new Dictionary<string, object> { { "full", full } };
        }

        /// <summary>An erase tells nothing of how far it has got, so all that can be said is that it is still going, and for how long.</summary>
        private sealed class ErasingReporter : IDisposable
        {
            private readonly Reporter inner;

            public ErasingReporter(Server.Call call)
            {
                inner = new Reporter(call, 0, "Erasing the disc");
                ticker = new Thread(Run) { IsBackground = true, Name = "erase-words" };
                ticker.Start();
            }

            private readonly Thread ticker;
            private volatile bool stop;

            private void Run()
            {
                while (!stop)
                {
                    inner.Say("Erasing the disc (" + inner.Seconds + " seconds so far)");
                    for (int i = 0; i < 10 && !stop; i++)
                    {
                        Thread.Sleep(100);
                    }
                }
            }

            public void Finish()
            {
                stop = true;
            }

            public void Dispose()
            {
                stop = true;
                ticker.Join(2000);
                inner.Dispose();
            }
        }

        // ------------------------------------------------------------------------------------- for the checks (MTK_DISC_TEST=1)

        /// <summary>Whether each IMAPI component can be made, and what the disc master says; opens no drive.</summary>
        internal static Dictionary<string, object> Probe()
        {
            var made = new Dictionary<string, object>();
            foreach (string id in new[]
            {
                "IMAPI2.MsftDiscMaster2", "IMAPI2.MsftDiscRecorder2", "IMAPI2.MsftDiscFormat2Data", "IMAPI2.MsftDiscFormat2TrackAtOnce",
                "IMAPI2.MsftDiscFormat2Erase", "IMAPI2FS.MsftFileSystemImage",
            })
            {
                try
                {
                    object instance = Create(id);
                    made[id] = instance != null;
                    Marshal.ReleaseComObject(instance);
                }
                catch (Exception exception)
                {
                    made[id] = Failures.Of(exception).Message;
                }
            }

            var reply = new Dictionary<string, object> { { "components", made } };
            var raws = new Dictionary<string, object>();
            foreach (string id in new[] { "IMAPI2.MsftDiscFormat2RawCD", RawCreatorProgId })
            {
                try
                {
                    object instance = Create(id);
                    raws[id] = instance != null;
                    Marshal.ReleaseComObject(instance);
                }
                catch (Exception exception)
                {
                    raws[id] = Failures.Of(exception).Message;
                }
            }

            try
            {
                object instance = CreateRawImageCreator();
                raws["image maker, by program id or class id"] = instance != null;
                Marshal.ReleaseComObject(instance);
            }
            catch (Exception exception)
            {
                raws["image maker, by program id or class id"] = Failures.Of(exception).Message;
            }

            reply["raw_components"] = raws;
            try
            {
                dynamic master = Create("IMAPI2.MsftDiscMaster2");
                reply["supported_environment"] = (bool)master.IsSupportedEnvironment;
                reply["recorders"] = (int)master.Count;
            }
            catch (Exception exception)
            {
                reply["master_error"] = Failures.Of(exception).Message;
            }

            return reply;
        }
    }
}
