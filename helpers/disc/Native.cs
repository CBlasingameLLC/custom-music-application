using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace MusicToolkit.Disc
{
    /// <summary>The few Windows calls that talk to an optical drive, and the numbers they need.</summary>
    internal static class Native
    {
        // CreateFile
        public const uint GenericRead = 0x80000000;
        public const uint ShareRead = 1;
        public const uint ShareWrite = 2;
        public const uint OpenExisting = 3;

        // Device controls. Each is CTL_CODE(device type << 16 | access << 14 | function << 2 | method), worked out in the README.
        public const uint IoctlCdromReadToc = 0x00024000;      // CDROM (2), read, function 0x000, buffered
        public const uint IoctlCdromRawRead = 0x0002403E;      // CDROM (2), read, function 0x00F, direct output
        public const uint IoctlStorageCheckVerify = 0x002D4800; // STORAGE (0x2D), read, function 0x200, buffered
        public const uint IoctlStorageMediaRemoval = 0x002D4804; // function 0x201
        public const uint IoctlStorageEjectMedia = 0x002D4808;  // function 0x202
        public const uint IoctlStorageQueryProperty = 0x002D1400; // any access, function 0x500

        public const uint DriveCdRom = 5; // GetDriveType

        /// <summary>The kind of data IOCTL_CDROM_RAW_READ is asked for. CDDA is plain audio: 2352 bytes a sector.</summary>
        public const uint TrackModeCdda = 2;

        [StructLayout(LayoutKind.Sequential)]
        public struct RawReadInfo
        {
            public long DiskOffset;   // the sector number times 2048 (what Windows calls a logical block), whatever the track holds
            public uint SectorCount;
            public uint TrackMode;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct StoragePropertyQuery
        {
            public uint PropertyId;  // 0 = the device's description
            public uint QueryType;   // 0 = ask for it
            public byte AdditionalParameters;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFile(
            string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes, uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool DeviceIoControl(
            SafeFileHandle device, uint controlCode, IntPtr inBuffer, uint inSize, IntPtr outBuffer, uint outSize, out uint bytesReturned, IntPtr overlapped);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern uint GetDriveType(string rootPathName);

        [DllImport("winmm.dll", CharSet = CharSet.Unicode)]
        public static extern int mciSendString(string command, System.Text.StringBuilder returnString, int returnLength, IntPtr callback);
    }
}
