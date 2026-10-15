using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace MusicToolkit.Disc
{
    /// <summary>Called between the small steps of something long: throws a refusal (code "cancelled") when the person asked to stop.</summary>
    internal delegate void Checkpoint();

    internal sealed class Track
    {
        public int Number;
        public bool Audio;
        public long Start; // the sector where it begins, counted from the start of the first track (what Windows calls the logical block)
    }

    internal sealed class Contents
    {
        public int First;
        public int Last;
        public long LeadOut; // the sector after the last one of the disc
        public readonly List<Track> Tracks = new List<Track>();

        public bool SameAs(Contents other)
        {
            if (other == null || LeadOut != other.LeadOut || Tracks.Count != other.Tracks.Count)
            {
                return false;
            }

            for (int i = 0; i < Tracks.Count; i++)
            {
                if (Tracks[i].Number != other.Tracks[i].Number || Tracks[i].Start != other.Tracks[i].Start || Tracks[i].Audio != other.Tracks[i].Audio)
                {
                    return false;
                }
            }

            return true;
        }
    }

    /// <summary>Which drives there are, and a check that a drive id is nothing but a drive letter before anything is opened.</summary>
    internal static class Drives
    {
        /// <summary>"E:" from what was asked: exactly one letter and a colon. A path, a device name or anything else is refused here, so that
        /// nothing a request says can ever name a device other than a lettered drive.</summary>
        public static string Normalize(object raw)
        {
            string text = (raw as string ?? string.Empty).Trim();
            bool letter = text.Length == 2 && text[1] == ':' && ((text[0] >= 'A' && text[0] <= 'Z') || (text[0] >= 'a' && text[0] <= 'z'));
            if (!letter)
            {
                throw new DiscFailure(Failures.NotFound, "That is not a drive letter.");
            }

            return char.ToUpperInvariant(text[0]) + ":";
        }

        /// <summary>The CD, DVD and Blu-ray drives that have a letter.</summary>
        public static List<string> Optical()
        {
            var found = new List<string>();
            foreach (DriveInfo drive in DriveInfo.GetDrives())
            {
                if (drive.DriveType == DriveType.CDRom && drive.Name.Length >= 2)
                {
                    found.Add(drive.Name.Substring(0, 2).ToUpperInvariant());
                }
            }

            return found;
        }

        /// <summary>Throws unless this is the letter of an optical drive. This is the only gate to opening a device.</summary>
        public static string Require(object raw)
        {
            string id = Normalize(raw);
            foreach (DriveInfo drive in DriveInfo.GetDrives())
            {
                if (drive.Name.Length >= 2 && string.Equals(drive.Name.Substring(0, 2), id, StringComparison.OrdinalIgnoreCase))
                {
                    if (drive.DriveType != DriveType.CDRom)
                    {
                        throw new DiscFailure(Failures.NotOptical, "That is not a CD or DVD drive.");
                    }

                    return id;
                }
            }

            throw new DiscFailure(Failures.NotFound, "That drive is not there any more.");
        }

        /// <summary>The make and model the drive reports about itself ("HL-DT-ST DVDRAM GH24NSD1"), or null.</summary>
        public static string HardwareName(string id)
        {
            try
            {
                using (SafeFileHandle handle = Native.CreateFile(@"\\.\" + id, 0, Native.ShareRead | Native.ShareWrite, IntPtr.Zero, Native.OpenExisting, 0, IntPtr.Zero))
                {
                    if (handle.IsInvalid)
                    {
                        return null;
                    }

                    int querySize = Marshal.SizeOf(typeof(Native.StoragePropertyQuery));
                    IntPtr query = Marshal.AllocHGlobal(querySize);
                    IntPtr answer = Marshal.AllocHGlobal(1024);
                    try
                    {
                        Marshal.StructureToPtr(new Native.StoragePropertyQuery(), query, false); // property 0 (the device), query 0 (describe it)
                        uint returned;
                        if (!Native.DeviceIoControl(handle, Native.IoctlStorageQueryProperty, query, (uint)querySize, answer, 1024, out returned, IntPtr.Zero) || returned < 32)
                        {
                            return null;
                        }

                        var data = new byte[returned];
                        Marshal.Copy(answer, data, 0, (int)returned);
                        string vendor = TextAt(data, BitConverter.ToInt32(data, 12)); // STORAGE_DEVICE_DESCRIPTOR.VendorIdOffset
                        string product = TextAt(data, BitConverter.ToInt32(data, 16)); // .ProductIdOffset
                        string name = (vendor + " " + product).Trim();
                        return name.Length == 0 ? null : name;
                    }
                    finally
                    {
                        Marshal.FreeHGlobal(query);
                        Marshal.FreeHGlobal(answer);
                    }
                }
            }
            catch (Exception)
            {
                return null; // the name is a courtesy; a drive without one is still a drive
            }
        }

        private static string TextAt(byte[] data, int offset)
        {
            if (offset <= 0 || offset >= data.Length)
            {
                return string.Empty;
            }

            int end = offset;
            while (end < data.Length && data[end] != 0)
            {
                end++;
            }

            return Encoding.ASCII.GetString(data, offset, end - offset).Trim();
        }
    }

    /// <summary>One optical drive held open: its contents, its audio sectors, the tray.</summary>
    internal sealed class Drive : IDisposable
    {
        public const int SectorBytes = 2352; // 588 stereo frames of 16 bits
        public const int MaxRead = 16;        // sectors in one request from Music Toolkit

        private const int LogicalBlock = 2048; // the unit of the sector numbers Windows takes, whatever a sector holds

        private readonly SafeFileHandle handle;
        private readonly IntPtr request;
        private readonly IntPtr audio;
        private int chunk = MaxRead; // the most sectors the drive (or the adapter it hangs off) has been seen to take at once
        private Contents known;      // what the disc held when it was last asked, to tell a changed disc from a disc Windows only re-checked

        public readonly string Id;

        private Drive(string id, SafeFileHandle handle)
        {
            Id = id;
            this.handle = handle;
            request = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(Native.RawReadInfo)));
            audio = Marshal.AllocHGlobal(MaxRead * SectorBytes);
        }

        /// <summary>Opens the drive with this letter, if it is an optical drive.</summary>
        public static Drive Open(object raw)
        {
            string id = Drives.Require(raw);
            SafeFileHandle handle = Native.CreateFile(@"\\.\" + id, Native.GenericRead, Native.ShareRead | Native.ShareWrite, IntPtr.Zero, Native.OpenExisting, 0, IntPtr.Zero);
            if (handle.IsInvalid)
            {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw Failures.Of(error, Failures.While.Opening);
            }

            return new Drive(id, handle);
        }

        public void Dispose()
        {
            handle.Dispose();
            Marshal.FreeHGlobal(request);
            Marshal.FreeHGlobal(audio);
        }

        private static void Pause(int milliseconds, Checkpoint check)
        {
            for (int waited = 0; waited < milliseconds; waited += 50)
            {
                if (check != null)
                {
                    check();
                }

                Thread.Sleep(50);
            }
        }

        /// <summary>Throws no_media when the tray is empty and not_ready while a disc is still spinning up (after waiting <paramref name="patience"/> tries of a quarter second).</summary>
        public void CheckMedia(int patience, Checkpoint check)
        {
            for (int attempt = 0; ; attempt++)
            {
                uint returned;
                if (Native.DeviceIoControl(handle, Native.IoctlStorageCheckVerify, IntPtr.Zero, 0, IntPtr.Zero, 0, out returned, IntPtr.Zero))
                {
                    return;
                }

                int error = Marshal.GetLastWin32Error();
                if (error == 1110 && attempt == 0)
                {
                    continue; // the first question after a disc changes always says so: that is news, not a problem
                }

                if (error == 21 && attempt < patience)
                {
                    Pause(250, check);
                    continue;
                }

                throw Failures.Of(error, Failures.While.Opening);
            }
        }

        /// <summary>The disc's table of contents. A disc that is still spinning up is waited for (<paramref name="patience"/> tries of a quarter second).</summary>
        public Contents ReadContents(int patience, Checkpoint check)
        {
            const int size = 804; // CDROM_TOC: a 4 byte header and room for 100 entries of 8 bytes
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                for (int attempt = 0; ; attempt++)
                {
                    for (int i = 0; i < size; i += 4)
                    {
                        Marshal.WriteInt32(buffer, i, 0);
                    }

                    uint returned;
                    if (Native.DeviceIoControl(handle, Native.IoctlCdromReadToc, IntPtr.Zero, 0, buffer, size, out returned, IntPtr.Zero))
                    {
                        var data = new byte[Math.Min((int)returned, size)];
                        Marshal.Copy(buffer, data, 0, data.Length);
                        known = Parse(data);
                        return known;
                    }

                    int error = Marshal.GetLastWin32Error();
                    if ((error == 21 && attempt < patience) || (error == 1110 && attempt < 2))
                    {
                        Pause(error == 21 ? 250 : 0, check);
                        continue;
                    }

                    throw Failures.Of(error, Failures.While.ReadingContents);
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        /// <summary>Reads the table the drive returned: a 4 byte header (length, first track, last track), then 8 bytes for every track and for the
        /// lead-out (track number 0xAA), whose last three bytes are the minute, second and frame where it starts. Windows' sector 0 is the frame 150
        /// into the disc, so 150 comes off.</summary>
        internal static Contents Parse(byte[] data)
        {
            if (data.Length < 4)
            {
                throw new DiscFailure(Failures.Unsupported, "The disc's table of contents could not be understood.");
            }

            int declared = ((data[0] << 8) | data[1]) + 2;
            int usable = Math.Min(declared, data.Length);
            var contents = new Contents { First = data[2], Last = data[3] };
            bool leadOut = false;
            for (int offset = 4; offset + 8 <= usable; offset += 8)
            {
                int control = data[offset + 1] & 0x0F;       // bit 2 set: a data track
                int number = data[offset + 2];
                long frames = ((data[offset + 5] * 60L) + data[offset + 6]) * 75L + data[offset + 7];
                long sector = frames - 150;
                if (number == 0xAA)
                {
                    contents.LeadOut = sector;
                    leadOut = true;
                }
                else if (number >= 1 && number <= 99)
                {
                    contents.Tracks.Add(new Track { Number = number, Audio = (control & 0x04) == 0, Start = sector });
                }
            }

            if (contents.Tracks.Count == 0 || !leadOut || contents.LeadOut <= contents.Tracks[contents.Tracks.Count - 1].Start)
            {
                throw new DiscFailure(Failures.Unsupported, "The disc's table of contents could not be understood.");
            }

            return contents;
        }

        /// <summary>Reads <paramref name="count"/> audio sectors (at most 16) and returns 2352 bytes of each.</summary>
        public byte[] ReadAudio(long lba, int count, Checkpoint check)
        {
            var result = new byte[count * SectorBytes];
            int done = 0;
            while (done < count)
            {
                int n = Math.Min(count - done, chunk);
                try
                {
                    ReadSectors(lba + done, n, check);
                }
                catch (DiscFailure failure)
                {
                    if (failure.Code == Failures.Unsupported && n > 1)
                    {
                        chunk = Math.Max(1, n / 2); // some drives and USB adapters take fewer sectors at once: remember, and go on with fewer
                        continue;
                    }

                    throw;
                }

                Marshal.Copy(audio, result, done * SectorBytes, n * SectorBytes);
                done += n;
            }

            return result;
        }

        /// <summary>Reads somewhere and throws it away, to push what the drive remembered of an earlier read out of its memory. Sectors that cannot
        /// be read do not matter here (only that the drive went there), so those are passed over.</summary>
        public void Flush(long lba, int count, Checkpoint check)
        {
            int done = 0;
            while (done < count)
            {
                if (check != null)
                {
                    check();
                }

                int n = Math.Min(count - done, Math.Min(chunk, MaxRead));
                try
                {
                    ReadSectors(lba + done, n, check);
                }
                catch (DiscFailure failure)
                {
                    if (failure.Code != Failures.ReadError && failure.Code != Failures.Unsupported)
                    {
                        throw;
                    }

                    if (failure.Code == Failures.Unsupported && n > 1)
                    {
                        chunk = Math.Max(1, n / 2);
                        continue;
                    }
                }

                done += n;
            }
        }

        /// <summary>One request to the drive, into the buffer.</summary>
        private void ReadSectors(long lba, int count, Checkpoint check)
        {
            uint size = (uint)(count * SectorBytes);
            bool rechecked = false;
            for (int attempt = 0; ; attempt++)
            {
                var info = new Native.RawReadInfo { DiskOffset = lba * LogicalBlock, SectorCount = (uint)count, TrackMode = Native.TrackModeCdda };
                Marshal.StructureToPtr(info, request, false);
                uint returned;
                if (Native.DeviceIoControl(handle, Native.IoctlCdromRawRead, request, (uint)Marshal.SizeOf(typeof(Native.RawReadInfo)), audio, size, out returned, IntPtr.Zero))
                {
                    if (returned != size)
                    {
                        throw new DiscFailure(Failures.ReadError, "The drive gave back less than it was asked for (sector " + lba + ").");
                    }

                    return;
                }

                int error = Marshal.GetLastWin32Error();
                if (error == 21 && attempt < 40)
                {
                    Pause(250, check); // still getting ready: the first read after a disc goes in can wait for it to spin up
                    continue;
                }

                if (error == 1110 && !rechecked)
                {
                    rechecked = true;
                    if (StillTheSameDisc(check))
                    {
                        continue; // Windows only noticed something (the tray, a program asking); the disc is the one that was being read
                    }
                }

                throw Failures.Of(error, Failures.While.ReadingAudio, "The drive could not read sector " + lba + ".");
            }
        }

        private bool StillTheSameDisc(Checkpoint check)
        {
            if (known == null)
            {
                return false;
            }

            try
            {
                CheckMedia(0, check);
                Contents before = known;
                Contents now = ReadContents(0, check);
                known = before;
                return before.SameAs(now);
            }
            catch (DiscFailure)
            {
                return false;
            }
        }

        /// <summary>Opens the tray (or pushes the disc out of a slot).</summary>
        public void Eject()
        {
            uint returned;
            IntPtr allow = Marshal.AllocHGlobal(1);
            try
            {
                Marshal.WriteByte(allow, 0); // PREVENT_MEDIA_REMOVAL.PreventMediaRemoval = FALSE: a program may have asked for the tray to be locked
                Native.DeviceIoControl(handle, Native.IoctlStorageMediaRemoval, allow, 1, IntPtr.Zero, 0, out returned, IntPtr.Zero);
            }
            finally
            {
                Marshal.FreeHGlobal(allow);
            }

            if (Native.DeviceIoControl(handle, Native.IoctlStorageEjectMedia, IntPtr.Zero, 0, IntPtr.Zero, 0, out returned, IntPtr.Zero))
            {
                return;
            }

            int error = Marshal.GetLastWin32Error();
            if (!OpenTrayThroughMultimedia(Id))
            {
                throw Failures.Of(error, Failures.While.Ejecting);
            }
        }

        /// <summary>The way any program may open a tray, for the drives that refuse the device control.</summary>
        private static bool OpenTrayThroughMultimedia(string id)
        {
            const string alias = "mtkdoor";
            try
            {
                if (Native.mciSendString("open " + id + " type cdaudio alias " + alias, null, 0, IntPtr.Zero) != 0)
                {
                    return false;
                }

                try
                {
                    return Native.mciSendString("set " + alias + " door open wait", null, 0, IntPtr.Zero) == 0;
                }
                finally
                {
                    Native.mciSendString("close " + alias, null, 0, IntPtr.Zero);
                }
            }
            catch (Exception)
            {
                return false;
            }
        }
    }
}
