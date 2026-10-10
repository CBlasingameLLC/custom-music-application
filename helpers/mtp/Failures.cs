using System;
using System.IO;
using MediaDevices;

namespace MusicToolkit.Mtp
{
    /// <summary>Turns what went wrong into the three codes Music Toolkit acts on, plus words for the person.</summary>
    internal static class Failures
    {
        internal const string NoSpace = "no_space";
        internal const string Disconnected = "disconnected";
        internal const string NotFound = "not_found";
        internal const string Failed = "failed";

        public static string CodeOf(Exception exception)
        {
            for (Exception e = exception; e != null; e = e.InnerException)
            {
                if (e is NotConnectedException)
                {
                    return Disconnected;
                }

                if (e is FileNotFoundException || e is DirectoryNotFoundException)
                {
                    return NotFound;
                }

                switch (unchecked((uint)e.HResult))
                {
                    case 0x8007048F: // ERROR_DEVICE_NOT_CONNECTED
                    case 0x800701A3: // ERROR_DEVICE_NOT_AVAILABLE
                    case 0x80070015: // ERROR_NOT_READY (the device went away or is not ready)
                    case 0x8007001F: // ERROR_GEN_FAILURE (what an unplugged cable most often looks like mid-copy)
                        return Disconnected;
                    case 0x80070070: // ERROR_DISK_FULL
                    case 0x80070027: // ERROR_HANDLE_DISK_FULL
                    case 0x80030070: // STG_E_MEDIUMFULL
                        return NoSpace;
                    case 0x80070002: // ERROR_FILE_NOT_FOUND
                    case 0x80070003: // ERROR_PATH_NOT_FOUND
                    case 0x80070490: // ERROR_NOT_FOUND
                        return NotFound;
                }
            }

            return Failed;
        }

        public static string MessageOf(Exception exception, string code)
        {
            switch (code)
            {
                case NoSpace:
                    return "There is not enough room on the device";
                case Disconnected:
                    return "The device was disconnected";
                case NotFound:
                    return "That was not found on the device";
            }

            string text = (exception.Message ?? exception.GetType().Name).Trim();
            return text.Length > 300 ? text.Substring(0, 300) : text;
        }
    }
}
