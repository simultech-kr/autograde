using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio;
using Microsoft.VisualStudio.Shell;
using Microsoft.VisualStudio.Shell.Interop;

namespace Autograde.VisualStudio
{
    internal static class WorkspaceOpener
    {
        public static async Task OpenAsync(string directory, CancellationToken cancel)
        {
            if (!Directory.Exists(directory)) throw new DirectoryNotFoundException();
            var fullPath = Path.GetFullPath(directory);
            await ThreadHelper.JoinableTaskFactory.SwitchToMainThreadAsync(cancel);
            cancel.ThrowIfCancellationRequested();
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
