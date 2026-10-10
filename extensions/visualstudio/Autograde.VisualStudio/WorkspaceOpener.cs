using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Autograde.Core;
using Microsoft.VisualStudio;
using Microsoft.VisualStudio.Shell;
using Microsoft.VisualStudio.Shell.Interop;

namespace Autograde.VisualStudio
{
    internal static class WorkspaceOpener
    {
        public static string CurrentDirectory()
        {
            ThreadHelper.ThrowIfNotOnUIThread();
            var solution = ServiceProvider.GlobalProvider.GetService(typeof(SVsSolution)) as IVsSolution;
            if (solution == null || ErrorHandler.Failed(solution.GetSolutionInfo(out var directory, out _, out _))) return null;
            return string.IsNullOrWhiteSpace(directory) ? null : directory;
        }

        public static async Task OpenAsync(string directory, CancellationToken cancel)
        {
            if (!Directory.Exists(directory)) throw new DirectoryNotFoundException();
            var fullPath = Path.GetFullPath(directory);
            await ThreadHelper.JoinableTaskFactory.SwitchToMainThreadAsync(cancel);
            cancel.ThrowIfCancellationRequested();
            // Keep an already-open solution inside this assignment intact.
            try
            {
                var opened = WorkspaceBinding.FromOpenDirectory(CurrentDirectory());
                if (opened != null && WorkspaceBinding.SamePath(opened.Root, fullPath)) return;
            }
            catch (Exception ex) when (ex is IOException || ex is InvalidDataException || ex is ArgumentException)
            { /* A broken old workspace must not prevent opening the requested valid folder. */ }
            // Use the IDE's folder API, not a shell command, auto-build, or an
            // arbitrary solution/project file discovered inside the archive.
            var solution = ServiceProvider.GlobalProvider.GetService(typeof(SVsSolution)) as IVsSolution7;
            if (solution == null) throw new InvalidOperationException("Visual Studio 폴더 열기를 사용할 수 없습니다.");
            solution.OpenFolder(fullPath);
            // A native save/trust dialog may cancel opening without throwing.
            // Do not claim success if the IDE stayed on the previous workspace.
            var current = (IVsSolution)solution;
            ErrorHandler.ThrowOnFailure(current.GetSolutionInfo(out var openedDirectory, out _, out _));
            if (string.IsNullOrEmpty(openedDirectory) ||
                !string.Equals(Path.GetFullPath(openedDirectory).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                    fullPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar), StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("요청한 과제 폴더가 열리지 않았습니다.");
        }
    }
}
