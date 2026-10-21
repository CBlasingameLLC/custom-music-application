using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace MusicToolkit.Disc
{
    /// <summary>Songs that Windows can decode but Python's audio library cannot (AAC and M4A, WMA ...), through Media Foundation, so that they can be
    /// written on a music CD. The interfaces are declared here by hand, like everything else in this program, so nothing is installed alongside it.
    ///
    /// A song is decoded to a WAV file (16-bit whole numbers where the decoder gives them, otherwise floating point), at the rate and with the channels
    /// the decoder itself gives; turning that into CD sound (44.1 kHz, 16 bit, stereo) is done in Python, the same way as for every other song.</summary>
    internal static class Decoder
    {
        private static readonly Guid MajorType = new Guid("48eba18e-f8c9-4687-bf11-0a74c9f96a8f");
        private static readonly Guid Subtype = new Guid("f7e34c9a-42e8-4714-b74b-cb29d72c35e5");
        private static readonly Guid Channels = new Guid("37e48bf5-645e-4c5b-89de-ada9e29b696a");
        private static readonly Guid Rate = new Guid("5faeeae7-0290-4c31-9e8a-c534f68d9dba");
        private static readonly Guid Bits = new Guid("f2deb57f-40fa-4764-aa33-ed4f2d1ff669");
        private static readonly Guid Duration = new Guid("6c990d33-bb8e-477a-8598-0d5d96fcd88a");
        private static readonly Guid TypeAudio = new Guid("73647561-0000-0010-8000-00aa00389b71");
        private static readonly Guid FormatPcm = new Guid("00000001-0000-0010-8000-00aa00389b71");
        private static readonly Guid FormatFloat = new Guid("00000003-0000-0010-8000-00aa00389b71");

        // Which streams a source reader is asked about
        private const int FirstAudioStream = unchecked((int)0xFFFFFFFD);
        private const int AllStreams = unchecked((int)0xFFFFFFFE);
        private const int MediaSource = unchecked((int)0xFFFFFFFF);

        // MF_SOURCE_READER_FLAG
        private const int ReaderError = 0x1;
        private const int ReaderEndOfStream = 0x2;
        private const int ReaderCurrentTypeChanged = 0x20;

        private const uint StartupVersion = 0x00020070; // MF_VERSION (SDK 2, API 0x70)
        private static bool started;

        // ------------------------------------------------------------------------------------- the interfaces (vtable order is what matters)

        [ComImport, Guid("44AE0FA8-EA31-4109-8D2E-4CAE4997C555"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IMFMediaType
        {
            // IMFAttributes
            void GetItem(ref Guid key, IntPtr value);
            void GetItemType(ref Guid key, out int type);
            void CompareItem(ref Guid key, IntPtr value, [MarshalAs(UnmanagedType.Bool)] out bool result);
            void Compare(IntPtr theirs, int matchType, [MarshalAs(UnmanagedType.Bool)] out bool result);
            void GetUINT32(ref Guid key, out int value);
            void GetUINT64(ref Guid key, out long value);
            void GetDouble(ref Guid key, out double value);
            void GetGUID(ref Guid key, out Guid value);
            void GetStringLength(ref Guid key, out int length);
            void GetString(ref Guid key, IntPtr value, int size, out int length);
            void GetAllocatedString(ref Guid key, out IntPtr value, out int length);
            void GetBlobSize(ref Guid key, out int size);
            void GetBlob(ref Guid key, IntPtr buffer, int size, out int sizeWritten);
            void GetAllocatedBlob(ref Guid key, out IntPtr buffer, out int size);
            void GetUnknown(ref Guid key, ref Guid riid, out IntPtr obj);
            void SetItem(ref Guid key, IntPtr value);
            void DeleteItem(ref Guid key);
            void DeleteAllItems();
            void SetUINT32(ref Guid key, int value);
            void SetUINT64(ref Guid key, long value);
            void SetDouble(ref Guid key, double value);
            void SetGUID(ref Guid key, ref Guid value);
            void SetString(ref Guid key, [MarshalAs(UnmanagedType.LPWStr)] string value);
            void SetBlob(ref Guid key, IntPtr buffer, int size);
            void SetUnknown(ref Guid key, IntPtr unknown);
            void LockStore();
            void UnlockStore();
            void GetCount(out int items);
            void GetItemByIndex(int index, out Guid key, IntPtr value);
            void CopyAllItems(IntPtr destination);

            // IMFMediaType
            void GetMajorType(out Guid type);
            void IsCompressedFormat([MarshalAs(UnmanagedType.Bool)] out bool compressed);
            [PreserveSig]
            int IsEqual(IntPtr other, out int flags);
            void GetRepresentation(Guid representation, out IntPtr result);
            void FreeRepresentation(Guid representation, IntPtr result);
        }

        [ComImport, Guid("C40A00F2-B93A-4D80-AE8C-5A1C634F58E4"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IMFSample
        {
            // IMFAttributes
            void GetItem(ref Guid key, IntPtr value);
            void GetItemType(ref Guid key, out int type);
            void CompareItem(ref Guid key, IntPtr value, [MarshalAs(UnmanagedType.Bool)] out bool result);
            void Compare(IntPtr theirs, int matchType, [MarshalAs(UnmanagedType.Bool)] out bool result);
            void GetUINT32(ref Guid key, out int value);
            void GetUINT64(ref Guid key, out long value);
            void GetDouble(ref Guid key, out double value);
            void GetGUID(ref Guid key, out Guid value);
            void GetStringLength(ref Guid key, out int length);
            void GetString(ref Guid key, IntPtr value, int size, out int length);
            void GetAllocatedString(ref Guid key, out IntPtr value, out int length);
            void GetBlobSize(ref Guid key, out int size);
            void GetBlob(ref Guid key, IntPtr buffer, int size, out int sizeWritten);
            void GetAllocatedBlob(ref Guid key, out IntPtr buffer, out int size);
            void GetUnknown(ref Guid key, ref Guid riid, out IntPtr obj);
            void SetItem(ref Guid key, IntPtr value);
            void DeleteItem(ref Guid key);
            void DeleteAllItems();
            void SetUINT32(ref Guid key, int value);
            void SetUINT64(ref Guid key, long value);
            void SetDouble(ref Guid key, double value);
            void SetGUID(ref Guid key, ref Guid value);
            void SetString(ref Guid key, [MarshalAs(UnmanagedType.LPWStr)] string value);
            void SetBlob(ref Guid key, IntPtr buffer, int size);
            void SetUnknown(ref Guid key, IntPtr unknown);
            void LockStore();
            void UnlockStore();
            void GetCount(out int items);
            void GetItemByIndex(int index, out Guid key, IntPtr value);
            void CopyAllItems(IntPtr destination);

            // IMFSample
            void GetSampleFlags(out int flags);
            void SetSampleFlags(int flags);
            void GetSampleTime(out long time);
            void SetSampleTime(long time);
            void GetSampleDuration(out long duration);
            void SetSampleDuration(long duration);
            void GetBufferCount(out int count);
            void GetBufferByIndex(int index, out IMFMediaBuffer buffer);
            void ConvertToContiguousBuffer(out IMFMediaBuffer buffer);
            void AddBuffer(IMFMediaBuffer buffer);
            void RemoveBufferByIndex(int index);
            void RemoveAllBuffers();
            void GetTotalLength(out int length);
            void CopyToBuffer(IMFMediaBuffer buffer);
        }

        [ComImport, Guid("045FA593-8799-42B8-BC8D-8968C6453507"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IMFMediaBuffer
        {
            void Lock(out IntPtr buffer, out int maxLength, out int currentLength);
            void Unlock();
            void GetCurrentLength(out int length);
            void SetCurrentLength(int length);
            void GetMaxLength(out int length);
        }

        [ComImport, Guid("70AE66F2-C809-4E4F-8915-BDCB406B7993"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IMFSourceReader
        {
            void GetStreamSelection(int stream, [MarshalAs(UnmanagedType.Bool)] out bool selected);
            void SetStreamSelection(int stream, [MarshalAs(UnmanagedType.Bool)] bool selected);
            void GetNativeMediaType(int stream, int index, out IMFMediaType type);
            void GetCurrentMediaType(int stream, out IMFMediaType type);
            void SetCurrentMediaType(int stream, IntPtr reserved, IMFMediaType type);
            void SetCurrentPosition(ref Guid timeFormat, IntPtr position);
            void ReadSample(int stream, int flags, out int actualStream, out int streamFlags, out long timestamp, out IMFSample sample);
            void Flush(int stream);
            void GetServiceForStream(int stream, ref Guid service, ref Guid riid, out IntPtr obj);
            void GetPresentationAttribute(int stream, ref Guid attribute, IntPtr value);
        }

        [DllImport("mfplat.dll")]
        private static extern int MFStartup(uint version, uint flags);

        [DllImport("mfplat.dll")]
        private static extern int MFCreateMediaType(out IMFMediaType type);

        [DllImport("mfreadwrite.dll", CharSet = CharSet.Unicode)]
        private static extern int MFCreateSourceReaderFromURL(string url, IntPtr attributes, out IMFSourceReader reader);

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(IntPtr value);

        // ------------------------------------------------------------------------------------- what Media Foundation says, in words

        /// <summary>What Media Foundation's error numbers mean for the person, and for the program (the kinds are the ones in src/musictoolkit/discs/helper.py).</summary>
        private static DiscFailure Fail(int code, string what)
        {
            uint number = unchecked((uint)code);
            switch (number)
            {
                case 0x80070002u: // ERROR_FILE_NOT_FOUND
                case 0x80070003u: // ERROR_PATH_NOT_FOUND
                    return new DiscFailure(Failures.Failed, what + " is not there.");
                case 0x80070020u: // ERROR_SHARING_VIOLATION
                case 0x80070005u: // E_ACCESSDENIED
                    return new DiscFailure(Failures.Busy, what + " cannot be opened: another program has it, or Windows would not allow it.");
                case 0xC00D36C4u: // MF_E_UNSUPPORTED_BYTESTREAM_TYPE
                    return new DiscFailure(Failures.Unsupported, "Windows cannot read this kind of file (" + what + ").");
                case 0xC00D36B3u: // MF_E_INVALIDSTREAMNUMBER (no sound in it)
                    return new DiscFailure(Failures.Unsupported, "There is no sound in " + what + " that Windows can read.");
                case 0xC00D36B4u: // MF_E_INVALIDMEDIATYPE
                case 0xC00D5212u: // MF_E_TOPO_CODEC_NOT_FOUND
                    return new DiscFailure(Failures.Unsupported, "Windows has no decoder for the sound in " + what + ". If this is a 'N' edition of Windows, the Media Feature Pack from Microsoft adds them.");
            }

            return new DiscFailure(Failures.Failed, "Windows could not read " + what + " (0x" + number.ToString("X8") + ").");
        }

        private static void Check(int code, string what)
        {
            if (code < 0)
            {
                throw Fail(code, what);
            }
        }

        private static void Startup()
        {
            if (started)
            {
                return;
            }

            try
            {
                Check(MFStartup(StartupVersion, 0), "Media Foundation");
            }
            catch (DllNotFoundException)
            {
                throw new DiscFailure(Failures.Unsupported, "This Windows has no Media Foundation, which decodes songs like these. A 'N' edition of Windows needs the Media Feature Pack from Microsoft.");
            }

            started = true;
        }

        private static string LocalFile(string path)
        {
            if (string.IsNullOrEmpty(path) || path.StartsWith(@"\\.\", StringComparison.Ordinal) || path.StartsWith(@"\\?\", StringComparison.Ordinal)
                || !Path.IsPathRooted(path) || !File.Exists(path))
            {
                throw new DiscFailure(Failures.Failed, (string.IsNullOrEmpty(path) ? "A song" : Path.GetFileName(path)) + " is not there.");
            }

            return path;
        }

        // ------------------------------------------------------------------------------------- opening a song

        private static IMFSourceReader Open(string path)
        {
            string name = Path.GetFileName(path);
            IMFSourceReader reader;
            try
            {
                Check(MFCreateSourceReaderFromURL(path, IntPtr.Zero, out reader), name);
            }
            catch (DllNotFoundException)
            {
                throw new DiscFailure(Failures.Unsupported, "This Windows has no Media Foundation, which decodes songs like these. A 'N' edition of Windows needs the Media Feature Pack from Microsoft.");
            }

            reader.SetStreamSelection(AllStreams, false);
            reader.SetStreamSelection(FirstAudioStream, true);
            return reader;
        }

        private static int Number(IMFMediaType type, Guid key)
        {
            int value;
            type.GetUINT32(ref key, out value);
            return value;
        }

        private static Guid Kind(IMFMediaType type, Guid key)
        {
            Guid value;
            type.GetGUID(ref key, out value);
            return value;
        }

        /// <summary>Asks the reader for plain sound: whole 16-bit numbers if the decoder gives them, floating point if not. Returns the type it settled on.</summary>
        private static IMFMediaType Negotiate(IMFSourceReader reader, string name)
        {
            COMException last = null;
            foreach (bool floating in new[] { false, true })
            {
                IMFMediaType want;
                Check(MFCreateMediaType(out want), name);
                try
                {
                    Guid key = MajorType;
                    Guid value = TypeAudio;
                    want.SetGUID(ref key, ref value);
                    key = Subtype;
                    value = floating ? FormatFloat : FormatPcm;
                    want.SetGUID(ref key, ref value);
                    if (!floating)
                    {
                        key = Bits;
                        want.SetUINT32(ref key, 16);
                    }

                    reader.SetCurrentMediaType(FirstAudioStream, IntPtr.Zero, want);
                    IMFMediaType got;
                    reader.GetCurrentMediaType(FirstAudioStream, out got);
                    return got;
                }
                catch (COMException error)
                {
                    last = error;
                }
                finally
                {
                    Marshal.ReleaseComObject(want);
                }
            }

            throw Fail(last.ErrorCode, name);
        }

        private static string CodecName(IMFMediaType native)
        {
            Guid format = Kind(native, Subtype);
            string text = format.ToString().ToLowerInvariant();
            if (text.EndsWith("-0000-0010-8000-00aa00389b71", StringComparison.Ordinal))
            {
                switch (text.Substring(0, 8))
                {
                    case "00001610": return "AAC";
                    case "00001600": return "AAC";
                    case "00000160": return "WMA";
                    case "00000161": return "WMA";
                    case "00000162": return "WMA Pro";
                    case "00000163": return "WMA Lossless";
                    case "00000055": return "MP3";
                    case "00000001": return "PCM";
                    case "00000003": return "PCM";
                    case "0000f1ac": return "FLAC";
                    case "63616c61": return "Apple Lossless";
                }
            }

            return "an audio format Windows knows (" + text.Substring(0, 8) + ")";
        }

        private static long DurationOf(IMFSourceReader reader)
        {
            IntPtr value = Marshal.AllocCoTaskMem(32);
            try
            {
                for (int i = 0; i < 32; i++)
                {
                    Marshal.WriteByte(value, i, 0);
                }

                Guid key = Duration;
                reader.GetPresentationAttribute(MediaSource, ref key, value);
                short vt = Marshal.ReadInt16(value);
                long ticks = vt == 21 ? Marshal.ReadInt64(value, 8) : 0; // VT_UI8: the length in units of 100 nanoseconds
                PropVariantClear(value);
                return ticks;
            }
            catch (COMException)
            {
                return 0; // a song of unknown length is still a song
            }
            finally
            {
                Marshal.FreeCoTaskMem(value);
            }
        }

        // ------------------------------------------------------------------------------------- what Windows can do with these songs

        /// <summary>For each song: whether Windows can decode it, and if so its rate, channels, length and kind. A song it cannot is said so, in
        /// words; one that cannot be read does not spoil the answer for the others.</summary>
        internal static List<object> Inspect(IList<string> paths)
        {
            var found = new List<object>();
            foreach (string path in paths)
            {
                var one = new Dictionary<string, object> { { "path", path } };
                IMFSourceReader reader = null;
                IMFMediaType native = null;
                IMFMediaType current = null;
                try
                {
                    string name = Path.GetFileName(path ?? string.Empty);
                    LocalFile(path);
                    Startup();
                    reader = Open(path);
                    reader.GetNativeMediaType(FirstAudioStream, 0, out native);
                    string codec = CodecName(native);
                    current = Negotiate(reader, name);
                    one["ok"] = true;
                    one["codec"] = codec;
                    one["rate"] = Number(current, Rate);
                    one["channels"] = Number(current, Channels);
                    one["bits"] = Number(current, Bits);
                    one["seconds"] = Math.Round(DurationOf(reader) / 1e7, 3);
                }
                catch (DiscFailure failure)
                {
                    one["ok"] = false;
                    one["code"] = failure.Code;
                    one["error"] = failure.Message;
                }
                catch (COMException error)
                {
                    DiscFailure failure = Fail(error.ErrorCode, Path.GetFileName(path ?? string.Empty));
                    one["ok"] = false;
                    one["code"] = failure.Code;
                    one["error"] = failure.Message;
                }
                finally
                {
                    foreach (object com in new object[] { current, native, reader })
                    {
                        if (com != null)
                        {
                            Marshal.ReleaseComObject(com);
                        }
                    }
                }

                found.Add(one);
            }

            return found;
        }

        // ------------------------------------------------------------------------------------- decoding a song to a WAV file

        private static void WriteHeader(Stream stream, int format, int channels, int rate, int bits, long dataBytes)
        {
            int blockAlign = channels * bits / 8;
            var header = new MemoryStream();
            var writer = new BinaryWriter(header);
            writer.Write(Encoding.ASCII.GetBytes("RIFF"));
            writer.Write((uint)(36 + dataBytes));
            writer.Write(Encoding.ASCII.GetBytes("WAVEfmt "));
            writer.Write(16);
            writer.Write((short)format);
            writer.Write((short)channels);
            writer.Write(rate);
            writer.Write(rate * blockAlign);
            writer.Write((short)blockAlign);
            writer.Write((short)bits);
            writer.Write(Encoding.ASCII.GetBytes("data"));
            writer.Write((uint)dataBytes);
            writer.Flush();
            stream.Write(header.ToArray(), 0, (int)header.Length);
        }

        /// <summary>Decodes a song to a WAV file at `output` (an absolute path ending in .wav, in a folder that exists, that is not a file already). The
        /// file is written as `output.part` and renamed when it is whole; a stop or a failure removes it.</summary>
        internal static Dictionary<string, object> Decode(string source, string output, Server.Call call)
        {
            source = LocalFile(source);
            if (string.IsNullOrEmpty(output) || !output.EndsWith(".wav", StringComparison.OrdinalIgnoreCase) || !Path.IsPathRooted(output)
                || output.StartsWith(@"\\.\", StringComparison.Ordinal) || output.StartsWith(@"\\?\", StringComparison.Ordinal))
            {
                throw new DiscFailure(Failures.Failed, "That is not a place a decoded song can be written.");
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

            call.Check(); // (a stop that is there before the song is opened)
            string name = Path.GetFileName(source);
            string part = output + ".part";
            bool made = false;
            IMFSourceReader reader = null;
            IMFMediaType type = null;
            try
            {
                Startup();
                reader = Open(source);
                type = Negotiate(reader, name);
                int channels = Number(type, Channels);
                int rate = Number(type, Rate);
                int bits = Number(type, Bits);
                bool floating = Kind(type, Subtype) == FormatFloat;
                if (channels < 1 || rate < 1 || bits < 8)
                {
                    throw new DiscFailure(Failures.Failed, "Windows did not say what the sound in " + name + " is like.");
                }

                long length = DurationOf(reader);
                long bytes = 0;
                long reported = Environment.TickCount;
                var buffer = new byte[1 << 16];
                using (var file = new FileStream(part, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 16))
                {
                    WriteHeader(file, floating ? 3 : 1, channels, rate, bits, 0);
                    while (true)
                    {
                        call.Check();
                        int actual;
                        int flags;
                        long stamp;
                        IMFSample sample;
                        reader.ReadSample(FirstAudioStream, 0, out actual, out flags, out stamp, out sample);
                        if ((flags & ReaderError) != 0)
                        {
                            throw new DiscFailure(Failures.Failed, "Windows could not decode " + name + " all the way to its end.");
                        }

                        if ((flags & ReaderCurrentTypeChanged) != 0)
                        {
                            IMFMediaType now;
                            reader.GetCurrentMediaType(FirstAudioStream, out now);
                            try
                            {
                                if (Number(now, Channels) != channels || Number(now, Rate) != rate || Number(now, Bits) != bits)
                                {
                                    throw new DiscFailure(Failures.Failed, "The sound in " + name + " changes in the middle (its rate or channels), which a music CD cannot follow.");
                                }
                            }
                            finally
                            {
                                Marshal.ReleaseComObject(now);
                            }
                        }

                        if (sample != null)
                        {
                            try
                            {
                                IMFMediaBuffer media;
                                sample.ConvertToContiguousBuffer(out media);
                                try
                                {
                                    IntPtr data;
                                    int max;
                                    int current;
                                    media.Lock(out data, out max, out current);
                                    try
                                    {
                                        for (int done = 0; done < current;)
                                        {
                                            int n = Math.Min(buffer.Length, current - done);
                                            Marshal.Copy(IntPtr.Add(data, done), buffer, 0, n);
                                            file.Write(buffer, 0, n);
                                            done += n;
                                        }

                                        bytes += current;
                                    }
                                    finally
                                    {
                                        media.Unlock();
                                    }
                                }
                                finally
                                {
                                    Marshal.ReleaseComObject(media);
                                }
                            }
                            finally
                            {
                                Marshal.ReleaseComObject(sample);
                            }

                            if (Environment.TickCount - reported >= 250)
                            {
                                reported = Environment.TickCount;
                                call.Progress(length > 0 ? Math.Min(stamp, length) : stamp, length, "Decoding " + name);
                            }
                        }

                        if ((flags & ReaderEndOfStream) != 0)
                        {
                            break;
                        }
                    }

                    if (bytes == 0)
                    {
                        throw new DiscFailure(Failures.Failed, "There is no sound in " + name + ".");
                    }

                    file.Seek(0, SeekOrigin.Begin);
                    WriteHeader(file, floating ? 3 : 1, channels, rate, bits, bytes);
                    file.Flush();
                }

                File.Move(part, output); // (refuses to replace a file that appeared in the meantime)
                made = true;
                call.Progress(length > 0 ? length : 1, length > 0 ? length : 1, "Decoded " + name);
                return new Dictionary<string, object>
                {
                    { "rate", rate }, { "channels", channels }, { "bits", bits }, { "float", floating }, { "frames", bytes / (channels * bits / 8) }, { "bytes", bytes },
                };
            }
            catch (COMException error)
            {
                throw Fail(error.ErrorCode, name);
            }
            finally
            {
                foreach (object com in new object[] { type, reader })
                {
                    if (com != null)
                    {
                        try
                        {
                            Marshal.ReleaseComObject(com);
                        }
                        catch (Exception)
                        {
                            // tidying up must never hide why the song was not decoded
                        }
                    }
                }

                if (!made)
                {
                    try
                    {
                        File.Delete(part);
                    }
                    catch (Exception)
                    {
                        // a program that is still looking at it holds it for a moment: it is tidied up with the folder
                    }
                }
            }
        }
    }
}
