using System;
using System.ComponentModel;

namespace MusicToolkit.Disc
{
    /// <summary>A refusal that Music Toolkit acts on: <see cref="Code"/> says what kind (the list is in src/musictoolkit/discs/helper.py)
    /// and the message is written for the person who will read it.</summary>
    internal sealed class DiscFailure : Exception
    {
        public readonly string Code;

        public DiscFailure(string code, string message) : base(message)
        {
            Code = code;
        }
    }

    /// <summary>What the person is told for each kind of refusal, and how Windows' numbers map onto the kinds.</summary>
    internal static class Failures
    {
        public const string NoMedia = "no_media";
        public const string MediaChanged = "media_changed";
        public const string NotReady = "not_ready";
        public const string ReadError = "read_error";
        public const string NotFound = "not_found";
        public const string NotOptical = "not_optical";
        public const string Busy = "busy";
        public const string Unsupported = "unsupported";
        public const string Cancelled = "cancelled";
        public const string Failed = "failed";
        public const string NotBlank = "not_blank";
        public const string WriteProtected = "write_protected";
        public const string NoSpace = "no_space";
        public const string Underrun = "underrun";

        /// <summary>What a Windows error means depends on what was being asked: "incorrect function" on a read means the drive or the track
        /// cannot do that, on a table-of-contents request it means much the same, and a bad sector is only a bad sector when reading.</summary>
        internal enum While
        {
            Opening,
            ReadingContents,
            ReadingAudio,
            Ejecting,
        }

        public static DiscFailure Of(int error, While what, string detail = null)
        {
            switch (error)
            {
                case 1112: // ERROR_NO_MEDIA_IN_DRIVE
                    return new DiscFailure(NoMedia, "There is no disc in the drive.");
                case 1110: // ERROR_MEDIA_CHANGED
                    return new DiscFailure(MediaChanged, "The disc was taken out or changed while it was being read.");
                case 21: // ERROR_NOT_READY
                    return new DiscFailure(NotReady, "The drive is not ready yet. Wait until the disc has stopped spinning up and try again.");
                case 2: // ERROR_FILE_NOT_FOUND
                case 3: // ERROR_PATH_NOT_FOUND
                case 20: // ERROR_BAD_UNIT
                case 55: // ERROR_DEV_NOT_EXIST
                case 433: // ERROR_NO_SUCH_DEVICE
                case 1167: // ERROR_DEVICE_NOT_CONNECTED
                case 4319: // ERROR_DEVICE_NOT_AVAILABLE
                    return new DiscFailure(NotFound, "That drive is not there any more.");
                case 5: // ERROR_ACCESS_DENIED
                case 32: // ERROR_SHARING_VIOLATION
                case 170: // ERROR_BUSY
                    return new DiscFailure(Busy, "Windows would not let Music Toolkit use the drive. Another program may have it to itself: close it and try again.");
                case 995: // ERROR_OPERATION_ABORTED
                    return new DiscFailure(Cancelled, "Stopped.");
                case 1785: // ERROR_UNRECOGNIZED_MEDIA
                case 1005: // ERROR_UNRECOGNIZED_VOLUME
                    return new DiscFailure(Unsupported, "The drive does not recognise this disc. It may be blank or not a music disc.");
            }

            if (what == While.ReadingAudio)
            {
                switch (error)
                {
                    case 23: // ERROR_CRC
                    case 25: // ERROR_SEEK
                    case 27: // ERROR_SECTOR_NOT_FOUND
                    case 30: // ERROR_READ_FAULT
                    case 31: // ERROR_GEN_FAILURE
                    case 121: // ERROR_SEM_TIMEOUT
                    case 996: // ERROR_IO_INCOMPLETE
                    case 1117: // ERROR_IO_DEVICE
                        return new DiscFailure(ReadError, detail ?? "The drive could not read part of the disc.");
                    case 1: // ERROR_INVALID_FUNCTION (the track is not audio, or the drive cannot read audio like this)
                    case 22: // ERROR_BAD_COMMAND
                    case 50: // ERROR_NOT_SUPPORTED
                    case 87: // ERROR_INVALID_PARAMETER
                        return new DiscFailure(Unsupported, "This drive cannot read this part of the disc as music. It may be a data track.");
                }
            }
            else if (what == While.ReadingContents)
            {
                switch (error)
                {
                    case 1: // ERROR_INVALID_FUNCTION
                    case 22: // ERROR_BAD_COMMAND
                    case 50: // ERROR_NOT_SUPPORTED
                    case 87: // ERROR_INVALID_PARAMETER
                    case 27: // ERROR_SECTOR_NOT_FOUND (what a blank disc most often answers)
                    case 31: // ERROR_GEN_FAILURE
                        return new DiscFailure(Unsupported, "The drive could not read the contents of this disc. It may be blank or damaged.");
                }
            }

            string windows;
            try
            {
                windows = new Win32Exception(error).Message;
            }
            catch (Exception)
            {
                windows = "error";
            }

            return new DiscFailure(Failed, "The drive reported a problem: " + windows.Trim().TrimEnd('.') + " (" + error + ").");
        }

        /// <summary>Anything that is not already a refusal is a slip in this program or in Windows: say so, as plainly as possible.</summary>
        public static DiscFailure Of(Exception exception)
        {
            var failure = exception as DiscFailure;
            if (failure != null)
            {
                return failure;
            }

            Exception inner = exception;
            while (inner is System.Reflection.TargetInvocationException && inner.InnerException != null)
            {
                inner = inner.InnerException;
            }

            var windows = inner as System.Runtime.InteropServices.COMException;
            if (windows != null)
            {
                return Imapi.Fail(windows);
            }

            string text = (exception.Message ?? exception.GetType().Name).Trim();
            if (text.Length > 300)
            {
                text = text.Substring(0, 300);
            }

            return new DiscFailure(Failed, exception.GetType().Name + ": " + text);
        }
    }
}
