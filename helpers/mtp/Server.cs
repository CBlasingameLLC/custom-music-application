using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;
using MediaDevices;

namespace MusicToolkit.Mtp
{
    /// <summary>Reads requests from Music Toolkit, does them on the one open device, and answers each in one line.</summary>
    internal sealed class Server
    {
        private sealed class Storage
        {
            public string Id;
            public string Name;
        }

        /// <summary>A place on the open device: the storage's name first, then the folders and the file.</summary>
        private sealed class Spot
        {
            public List<string> Parts;
            public string Full; // \Storage name\Music\Artist\song.mp3, the form the MediaDevices library takes
        }

        // Copying needs write access. Others (Explorer, Photos) may keep the device open at the same time.
        private const MediaDeviceAccess ReadWrite = (MediaDeviceAccess)((uint)MediaDeviceAccess.GenericRead | (uint)MediaDeviceAccess.GenericWrite);
        private const MediaDeviceShare Sharing = (MediaDeviceShare)((uint)MediaDeviceShare.Read | (uint)MediaDeviceShare.Write);

        private readonly TextReader input;
        private readonly TextWriter output;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
        private MediaDevice device;
        private Tree tree;
        private List<Storage> storages = new List<Storage>();

        public Server(TextReader input, TextWriter output)
        {
            this.input = input;
            this.output = output;
        }

        public int Run()
        {
            Send(new Dictionary<string, object> { { "event", "ready" }, { "version", Program.ProtocolVersion } });
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

                object id;
                request.TryGetValue("id", out id);
                string command = Text(request, "cmd");
                if (command == "close")
                {
                    Send(new Dictionary<string, object> { { "id", id }, { "ok", true } });
                    break;
                }

                Dictionary<string, object> reply;
                try
                {
                    reply = Handle(command, request);
                }
                catch (Exception exception)
                {
                    Console.Error.WriteLine(command + " failed: " + exception);
                    reply = Refusal(exception);
                }

                reply["id"] = id;
                Send(reply);
            }

            CloseDevice();
            return 0;
        }

        private void Send(Dictionary<string, object> message)
        {
            output.WriteLine(json.Serialize(message));
        }

        // ------------------------------------------------------------------------------------- commands

        private Dictionary<string, object> Handle(string command, Dictionary<string, object> request)
        {
            switch (command)
            {
                case "devices":
                    return Ok(new Dictionary<string, object> { { "devices", ListDevices() } });
                case "open":
                    return Open(Text(request, "device"));
                case "free":
                    return Free(Text(request, "storage"));
                case "stat":
                    return Stat(Text(request, "storage"), Text(request, "path"));
                case "list":
                    return List(Text(request, "storage"), Text(request, "path"));
                case "mkdir":
                    OpenDevice();
                    tree.Open(SpotOf(Text(request, "storage"), Text(request, "path")).Parts, true);
                    return Ok();
                case "delete":
                    return Delete(Text(request, "storage"), Text(request, "path"));
                case "put":
                    return Put(Text(request, "storage"), Text(request, "path"), Text(request, "source"));
                default:
                    return Failure(Failures.Failed, "unknown command " + command);
            }
        }

        private List<object> ListDevices()
        {
            var found = new List<object>();
            foreach (MediaDevice candidate in MediaDevice.GetDevices())
            {
                using (candidate)
                {
                    found.Add(Describe(candidate));
                }
            }

            return found;
        }

        /// <summary>What the Add a device dialog needs to know about one device. Opens it for a moment if it is not open.</summary>
        private static Dictionary<string, object> Describe(MediaDevice candidate)
        {
            string name = FirstText(candidate.FriendlyName, candidate.Description, "Device");
            var info = new Dictionary<string, object>
            {
                { "id", candidate.DeviceId },
                { "name", name },
                { "manufacturer", candidate.Manufacturer ?? string.Empty },
                { "model", string.Empty },
                { "serial", Synthetic(candidate, name) },
                { "storages", new List<object>() },
            };
            bool opened = false;
            try
            {
                if (!candidate.IsConnected)
                {
                    candidate.Connect(MediaDeviceAccess.GenericRead, Sharing);
                    opened = true;
                }

                info["model"] = candidate.Model ?? string.Empty;
                string serial = (candidate.SerialNumber ?? string.Empty).Trim();
                if (serial.Length > 0)
                {
                    info["serial"] = serial;
                }

                info["storages"] = new Tree(candidate).Storages().Select(s => StorageInfo(candidate, s.Folder.Id, s.Name)).ToList();
            }
            catch (Exception exception)
            {
                // Found, but would not open (locked, charging only, busy): say so, the person can fix it and look again.
                info["error"] = Failures.MessageOf(exception, Failures.CodeOf(exception));
            }
            finally
            {
                if (opened)
                {
                    try
                    {
                        candidate.Disconnect();
                    }
                    catch (Exception)
                    {
                    }
                }
            }

            return info;
        }

        /// <summary>A device with no serial number is told apart by what it is called, so it can be found again.</summary>
        private static string Synthetic(MediaDevice candidate, string name)
        {
            string text = string.Join("|", candidate.Manufacturer, candidate.Description, name);
            using (var sha = SHA1.Create())
            {
                byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(text));
                return "x-" + BitConverter.ToString(hash, 0, 6).Replace("-", string.Empty).ToLowerInvariant();
            }
        }

        private static string FirstText(params string[] candidates)
        {
            return candidates.FirstOrDefault(c => !string.IsNullOrWhiteSpace(c)) ?? string.Empty;
        }

        private static Dictionary<string, object> StorageInfo(MediaDevice opened, string id, string name)
        {
            var info = new Dictionary<string, object> { { "id", id }, { "name", name }, { "capacity", null }, { "free", null } };
            try
            {
                MediaStorageInfo details = opened.GetStorageInfo(id);
                if (details != null)
                {
                    info["capacity"] = (long)details.Capacity;
                    info["free"] = (long)details.FreeSpaceInBytes;
                }
            }
            catch (Exception)
            {
                // some devices do not say; the numbers stay unknown rather than wrong
            }

            return info;
        }

        private Dictionary<string, object> Open(string deviceId)
        {
            CloseDevice();
            MediaDevice found = MediaDevice.GetDevices().FirstOrDefault(d => d.DeviceId == deviceId);
            if (found == null)
            {
                return Failure(Failures.NotFound, "That device is not connected");
            }

            try
            {
                found.Connect(ReadWrite, Sharing);
                tree = new Tree(found);
                storages = tree.Storages().Select(s => new Storage { Id = s.Folder.Id, Name = s.Name }).ToList();
            }
            catch (Exception)
            {
                tree = null;
                found.Dispose();
                throw;
            }

            device = found;
            return Ok();
        }

        private MediaDevice OpenDevice()
        {
            if (device == null || tree == null)
            {
                throw new InvalidOperationException("open a device first");
            }

            return device;
        }

        private void CloseDevice()
        {
            MediaDevice old = device;
            device = null;
            tree = null;
            storages = new List<Storage>();
            if (old == null)
            {
                return;
            }

            try
            {
                if (old.IsConnected)
                {
                    old.Disconnect();
                }
            }
            catch (Exception)
            {
            }

            old.Dispose();
        }

        private Dictionary<string, object> Free(string storageId)
        {
            MediaStorageInfo details = OpenDevice().GetStorageInfo(StorageNamed(storageId).Id);
            if (details == null)
            {
                return Failure(Failures.Disconnected, "The storage is not available");
            }

            return Ok(new Dictionary<string, object> { { "free", (long)details.FreeSpaceInBytes }, { "capacity", (long)details.Capacity } });
        }

        private Dictionary<string, object> Stat(string storageId, string relative)
        {
            OpenDevice();
            Spot spot = SpotOf(storageId, relative);
            Entry entry = tree.Find(spot.Parts, false);
            if (entry == null)
            {
                return Ok(new Dictionary<string, object> { { "exists", false }, { "is_dir", false }, { "size", null } });
            }

            return Ok(new Dictionary<string, object> { { "exists", true }, { "is_dir", entry.IsDir }, { "size", entry.IsDir ? null : (object)entry.Size } });
        }

        private Dictionary<string, object> List(string storageId, string relative)
        {
            OpenDevice();
            Node folder = tree.Open(SpotOf(storageId, relative).Parts, false);
            if (folder == null)
            {
                return Failure(Failures.NotFound, "No such folder");
            }

            Tree.Load(folder);
            var entries = new List<object>();
            foreach (Entry entry in folder.Children.Values.OrderBy(e => e.Name, StringComparer.OrdinalIgnoreCase))
            {
                entries.Add(new Dictionary<string, object>
                {
                    { "name", entry.Name },
                    { "is_dir", entry.IsDir },
                    { "size", entry.IsDir ? null : (object)entry.Size },
                });
            }

            return Ok(new Dictionary<string, object> { { "entries", entries } });
        }

        private Dictionary<string, object> Delete(string storageId, string relative)
        {
            MediaDevice opened = OpenDevice();
            Spot spot = SpotOf(storageId, relative);
            if (spot.Parts.Count < 2)
            {
                throw new ArgumentException("a storage cannot be deleted");
            }

            // A copy that broke off may have left a file this session never listed, so a miss looks at the device again.
            Entry entry = tree.Find(spot.Parts, true);
            if (entry == null)
            {
                return Ok(); // already gone is as good as deleted
            }

            if (entry.IsDir)
            {
                opened.DeleteDirectory(spot.Full, true);
            }
            else
            {
                opened.DeleteFile(spot.Full);
            }

            tree.Remove(spot.Parts);
            return Ok();
        }

        private Dictionary<string, object> Put(string storageId, string relative, string source)
        {
            MediaDevice opened = OpenDevice();
            Spot spot = SpotOf(storageId, relative);
            if (spot.Parts.Count < 2)
            {
                throw new ArgumentException("a file needs a folder to go in");
            }

            long size = new FileInfo(source).Length;
            string name = spot.Parts[spot.Parts.Count - 1];
            List<string> folderParts = spot.Parts.Take(spot.Parts.Count - 1).ToList();
            Node folder = null;
            try
            {
                folder = tree.Open(folderParts, true);
                Tree.Load(folder);
                Entry old;
                if (folder.Children.TryGetValue(name, out old))
                {
                    if (old.IsDir)
                    {
                        throw new IOException("A folder called " + name + " is in the way");
                    }

                    opened.DeleteFile(spot.Full); // the library refuses to write over a file; replacing means removing first
                    folder.Children.Remove(name);
                }

                using (FileStream data = File.OpenRead(source))
                {
                    opened.UploadFile(data, spot.Full);
                }

                tree.Add(folder, name, size);
                return Ok(new Dictionary<string, object> { { "size", size } });
            }
            catch (Exception exception)
            {
                if (folder != null)
                {
                    folder.Children = null; // what is in there now is unknown (a half-written file?): read it again next time
                }

                // A generic failure while the file is larger than what is free is, in practice, a full device.
                if (Failures.CodeOf(exception) == Failures.Failed && size > FreeOn(storageId))
                {
                    return Failure(Failures.NoSpace, "There is not enough room on the device");
                }

                throw;
            }
        }

        private long FreeOn(string storageId)
        {
            try
            {
                MediaStorageInfo details = OpenDevice().GetStorageInfo(StorageNamed(storageId).Id);
                return details == null ? long.MaxValue : (long)details.FreeSpaceInBytes;
            }
            catch (Exception)
            {
                return long.MaxValue; // unknown: do not claim "full" on a guess
            }
        }

        // ------------------------------------------------------------------------------------- helpers

        private Storage StorageNamed(string storageId)
        {
            Storage found = storages.FirstOrDefault(s => s.Id == storageId);
            if (found == null)
            {
                throw new DirectoryNotFoundException("The device has no storage " + storageId);
            }

            return found;
        }

        /// <summary>Where a request points. Nothing may climb out of the storage.</summary>
        private Spot SpotOf(string storageId, string relative)
        {
            var parts = new List<string> { StorageNamed(storageId).Name };
            foreach (string part in (relative ?? string.Empty).Replace('\\', '/').Split('/'))
            {
                if (part.Length == 0)
                {
                    continue;
                }

                if (part == "." || part == "..")
                {
                    throw new ArgumentException("path leaves the storage");
                }

                parts.Add(part);
            }

            return new Spot { Parts = parts, Full = "\\" + string.Join("\\", parts) };
        }

        private static string Text(Dictionary<string, object> request, string key)
        {
            object value;
            return request.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : string.Empty;
        }

        private static Dictionary<string, object> Ok(Dictionary<string, object> extra = null)
        {
            var reply = new Dictionary<string, object> { { "ok", true } };
            if (extra != null)
            {
                foreach (KeyValuePair<string, object> pair in extra)
                {
                    reply[pair.Key] = pair.Value;
                }
            }

            return reply;
        }

        private static Dictionary<string, object> Failure(string code, string message)
        {
            return new Dictionary<string, object> { { "ok", false }, { "code", code }, { "error", message } };
        }

        private static Dictionary<string, object> Refusal(Exception exception)
        {
            string code = Failures.CodeOf(exception);
            return Failure(code, Failures.MessageOf(exception, code));
        }
    }
}
