using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MediaDevices;

namespace MusicToolkit.Mtp
{
    /// <summary>One thing inside a folder on the device.</summary>
    internal sealed class Entry
    {
        public string Name;
        public bool IsDir;
        public long Size;
        public MediaDirectoryInfo Folder; // the folder itself, when this is one
        public Node Node; // what is inside it, once it has been looked at
    }

    /// <summary>A folder on the device and, once read, what is in it.</summary>
    internal sealed class Node
    {
        public MediaDirectoryInfo Info;
        public Dictionary<string, Entry> Children; // null until the folder has been read
    }

    /// <summary>
    /// The device's folders as far as this session has looked, kept in memory. Windows Portable Devices has no way to ask for
    /// "the file called x in this folder": the MediaDevices library finds a path by reading every folder on the way and
    /// asking the device about each thing in it, over USB, every time. Reading each folder once, and answering the rest
    /// from here, is what keeps a few thousand songs from taking hours. A new helper (one per preview or sync) starts empty,
    /// so what was changed on the device in between is always seen.
    /// </summary>
    internal sealed class Tree
    {
        private readonly MediaDevice device;
        private readonly Node root;

        public Tree(MediaDevice device)
        {
            this.device = device;
            root = new Node { Info = device.GetRootDirectory() };
        }

        /// <summary>The storages ("Internal shared storage", "SD card") at the top of the device.</summary>
        public IEnumerable<Entry> Storages()
        {
            Load(root);
            List<Entry> all = root.Children.Values.Where(e => e.IsDir && e.Folder != null).ToList();
            try
            {
                // The device says which of its top-level objects are storages (others may be a phone or a camera's functions).
                MediaDriveInfo[] drives = device.GetDrives();
                var ids = new HashSet<string>(
                    (drives ?? new MediaDriveInfo[0]).Where(d => d != null && d.RootDirectory != null).Select(d => d.RootDirectory.Id));
                List<Entry> only = all.Where(e => ids.Contains(e.Folder.Id)).ToList();
                if (only.Count > 0)
                {
                    return only;
                }
            }
            catch (Exception)
            {
                // a device that does not say: every top-level folder is taken for a storage
            }

            return all;
        }

        /// <summary>The folder at this path (storage name first), or null if there is none. With create, makes what is missing.</summary>
        public Node Open(IList<string> parts, bool create)
        {
            Node node = root;
            foreach (string part in parts)
            {
                Load(node);
                Entry entry;
                if (!node.Children.TryGetValue(part, out entry))
                {
                    if (!create)
                    {
                        return null;
                    }

                    entry = Make(node, part);
                }
                else if (!entry.IsDir)
                {
                    if (!create)
                    {
                        return null;
                    }

                    throw new IOException("There is a file called " + part + " where a folder is needed");
                }

                node = entry.Node ?? (entry.Node = new Node { Info = entry.Folder });
            }

            return node;
        }

        /// <summary>The file or folder at this path, or null. A miss reads the folder again once if asked (it may be stale).</summary>
        public Entry Find(IList<string> parts, bool rereadOnMiss)
        {
            Node parent = Open(parts.Take(parts.Count - 1).ToList(), false);
            if (parent == null)
            {
                return null;
            }

            Load(parent);
            string name = parts[parts.Count - 1];
            Entry found;
            if (parent.Children.TryGetValue(name, out found))
            {
                return found;
            }

            if (!rereadOnMiss)
            {
                return null;
            }

            parent.Children = null;
            Load(parent);
            return parent.Children.TryGetValue(name, out found) ? found : null;
        }

        /// <summary>Forget what was read of a folder, so the next look asks the device again.</summary>
        public void Reread(IList<string> parts)
        {
            Node node = Open(parts, false);
            if (node != null)
            {
                node.Children = null;
            }
        }

        public void Remove(IList<string> parts)
        {
            Node parent = Open(parts.Take(parts.Count - 1).ToList(), false);
            if (parent != null && parent.Children != null)
            {
                parent.Children.Remove(parts[parts.Count - 1]);
            }
        }

        public void Add(Node folder, string name, long size)
        {
            Load(folder);
            folder.Children[name] = new Entry { Name = name, IsDir = false, Size = size };
        }

        public static void Load(Node node)
        {
            if (node.Children != null)
            {
                return;
            }

            // Case does not make two names different (the device's file system is usually FAT or case-insensitive).
            var children = new Dictionary<string, Entry>(StringComparer.OrdinalIgnoreCase);
            foreach (MediaFileSystemInfo info in node.Info.EnumerateFileSystemInfos())
            {
                string name = info.Name;
                if (string.IsNullOrEmpty(name) || children.ContainsKey(name))
                {
                    continue;
                }

                var folder = info as MediaDirectoryInfo;
                children[name] = new Entry
                {
                    Name = name,
                    IsDir = folder != null,
                    Size = folder != null ? 0 : (long)info.Length,
                    Folder = folder,
                };
            }

            node.Children = children;
        }

        private static Entry Make(Node parent, string name)
        {
            MediaDirectoryInfo made;
            string id;
            try
            {
                made = parent.Info.CreateSubdirectory(name);
                id = made == null ? null : made.Id; // a folder the device refused comes back as an empty shell
            }
            catch (NullReferenceException)
            {
                id = null;
                made = null;
            }

            if (string.IsNullOrEmpty(id))
            {
                throw new IOException("The device would not create the folder " + name);
            }

            var entry = new Entry { Name = name, IsDir = true, Folder = made };
            parent.Children[name] = entry;
            return entry;
        }
    }
}
