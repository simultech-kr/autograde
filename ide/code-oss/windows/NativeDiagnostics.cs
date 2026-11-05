// Pure .NET diagnostics: safe to format on any OS, including before session ACLs exist.
// Only fixed descriptions and exception metadata are emitted; never student paths,
// arbitrary exception messages, stack traces, environment variables or credentials.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Text;

namespace Autograde.NativeWindows
{
    internal sealed class NativeStageException : Exception
    {
        public string Stage { get; private set; }
        public NativeStageException(string stage, Exception cause)
            : base("Autograde native stage failed.", cause)
        { Stage = NativeDiagnostics.StageKey(stage); }
    }

    internal static class NativeDiagnostics
    {
        private const int MaxFailures = 8, MaxDepth = 8;
        private static readonly Dictionary<string, string> Stages = new Dictionary<string, string> {
            { "package", "IDE 설치 폴더 확인" },
            { "package-node", "실행 파일 확인 (runtime/node.exe)" },
            { "package-bootstrap", "시작 파일 확인 (autograde/windows-native-bootstrap.mjs)" },
            { "package-manifest", "패키지 정보 확인 (windows-package.json)" },
            { "session-recovery", "이전 실습 작업 폴더 확인·복구" },
            { "session-create", "새 실습 작업 공간 생성" },
            { "toolchain", "Windows C/C++ 개발 도구 준비" },
            { "process-create", "실습 프로세스 관리 준비" },
            { "runtime", "IDE 실행·동작" },
            { "process-stop", "실습 프로세스 종료 확인" },
            { "session-cleanup", "실습 임시 파일 정리" },
            { "unknown", "실행기 내부 처리" }
        };

        private sealed class Explanation
        {
            internal readonly string Code, Reason, Action;
            internal Explanation(string code, string reason, string action)
            { Code = code; Reason = reason; Action = action; }
        }

        private static readonly Dictionary<string, Explanation> Known = KnownFailures();
        private static Dictionary<string, Explanation> KnownFailures()
        {
            var result = new Dictionary<string, Explanation>(StringComparer.Ordinal);
            Add(result, "linked-path", "연결된 폴더 또는 파일을 감지했습니다.", "앱 전체를 링크가 아닌 로컬 폴더에 풀어 실행하세요. 작업 폴더의 링크는 담당자가 확인해야 합니다.",
                "Linked path is not allowed.");
            Add(result, "ntfs-required", "실습 작업 공간이 로컬 NTFS 디스크에 있지 않습니다.", "Windows 사용자 프로필의 로컬 NTFS 저장 위치를 확인하세요.",
                "Native sessions require a local NTFS directory.");
            Add(result, "owner-mismatch", "실습 폴더의 소유 정보가 현재 사용자 또는 복구 기록과 일치하지 않습니다.", "같은 Windows 계정으로 실행했는지 확인하고 담당자에게 폴더 권한과 소유 기록 확인을 요청하세요. 소유 기록을 임의로 삭제하지 마세요.",
                "Session directory owner changed.", "Native runtime ownership changed.", "Session ownership changed.",
                "Fixed working directory marker differs from its parent proof.", "Fixed working directory was replaced.");
            Add(result, "permissions", "실습 폴더의 사용자 접근 권한을 확인하지 못했습니다.", "담당자에게 실습 폴더의 현재 사용자 접근 권한을 확인해 달라고 요청하세요.",
                "Session directory grants another user access.", "Session directory lacks private control.");
            Add(result, "owner-record", "이전 실습의 소유·복구 기록이 없거나 올바르지 않습니다.", "기존 자료를 보존한 상태로 담당자에게 복구 기록 확인을 요청하세요.",
                "Invalid ownership marker.", "Invalid session cleanup policy.", "Fixed working directory identity is missing.", "Unexpected session directory.",
                "Unmarked working directory is not empty.", "Cannot identify the managed directory.");
            Add(result, "workspace-missing", "필요한 실습 폴더를 찾지 못했습니다.", "실습 폴더를 옮기거나 삭제한 적이 있는지 확인하고 다시 실행하세요.",
                "Private directory is missing.");
            Add(result, "cleanup-incomplete", "이전 실습의 작업 공간 정리가 완료되지 않았습니다.", "이전 Autograde IDE와 해당 실습 파일을 사용하는 프로그램을 종료한 뒤 다시 실행하세요.",
                "Previous working directory cleanup is incomplete.", "Session cleanup was not completed.");
            Add(result, "launcher-running", "이전 Autograde 실행기가 아직 실행 중입니다.", "열려 있는 Autograde IDE를 정상 종료한 뒤 다시 실행하세요.",
                "A previous native launcher is still running.");
            Add(result, "containment-unconfirmed", "이전 실습 프로세스의 종료·관리 상태를 확정하지 못했습니다.", "담당자가 남은 Autograde 프로세스를 확인한 뒤 재시도하세요. 확인 전에는 작업 공간을 정리하지 않습니다.",
                "A previous launch stopped before process containment was confirmed; operator recovery is required.",
                "An uncontained process could still exist; operator recovery is required.");
            Add(result, "process-control", "Windows 실습 프로세스 관리 작업이 실패했습니다.", "아래 Windows 오류 번호를 담당자에게 전달하고 실행 권한이나 보안 프로그램의 차단 여부를 확인하세요.",
                "Cannot reserve a private job.", "Cannot contain the Windows session.", "Cannot inspect a previous job.",
                "Cannot supervise the Windows session.", "Cannot resume the contained runtime.", "Cannot wait for the session.",
                "Cannot read the session result.", "Cannot stop session processes.", "Cannot verify stopped processes.");
            Add(result, "runtime-start", "번들 실행 프로그램을 시작하지 못했습니다.", "runtime/node.exe가 포함된 전체 패키지를 사용하고 Windows 오류 번호와 보안 프로그램 차단 기록을 확인하세요.",
                "Cannot start the bundled runtime.");
            Add(result, "process-stop-timeout", "실습 프로세스가 제한 시간 안에 모두 종료되지 않았습니다.", "잠시 기다린 뒤 이전 Autograde 창과 실습 프로그램의 종료 상태를 확인하세요.",
                "Session processes are still stopping; cleanup is blocked.");
            Add(result, "toolchain-missing", "MSVC C/C++ 도구 또는 Windows SDK를 찾지 못했습니다.", "Visual Studio Installer에서 C++ 데스크톱 개발, MSVC x64 도구와 Windows SDK 설치 상태를 확인하세요.",
                "MSVC C++ 도구와 Windows SDK가 필요합니다. 담당자가 Visual Studio Build Tools의 C++ 데스크톱 개발을 먼저 설치해 주세요.",
                "MSVC x64 도구 설치를 확인해 주세요.",
                "MSVC와 Windows SDK 헤더·라이브러리 설치가 완료되지 않았습니다. 담당자에게 확인해 주세요.");
            Add(result, "toolchain-environment", "Windows 개발 도구의 실행 환경을 준비하지 못했습니다.", "Visual Studio 개발자 명령 프롬프트가 정상적으로 열리는지 담당자가 확인해 주세요.",
                "Windows 개발 도구 환경을 준비하지 못했습니다. 담당자에게 확인해 주세요.");
            Add(result, "toolchain-timeout", "Windows 개발 도구 확인이 제한 시간을 초과했습니다.", "다른 설치·업데이트 작업이 끝난 뒤 다시 실행하고 반복되면 Visual Studio 설치 상태를 확인하세요.",
                "Toolchain discovery timed out.");
            Add(result, "invalid-environment", "실행기 환경 값의 형식이 올바르지 않습니다.", "담당자에게 Windows 환경 변수 설정 확인을 요청하세요. 진단에는 환경 변수 값을 기록하지 않습니다.",
                "Invalid environment.");
            return result;
        }

        private static void Add(Dictionary<string, Explanation> target, string code, string reason, string action, params string[] messages)
        {
            var explanation = new Explanation(code, reason, action);
            foreach (string message in messages) target.Add(message, explanation);
        }

        internal static string StageKey(string stage) { return stage != null && Stages.ContainsKey(stage) ? stage : "unknown"; }
        public static T Run<T>(string stage, Func<T> action)
        {
            try { return action(); }
            catch (NativeStageException) { throw; }
            catch (Exception error) { throw new NativeStageException(stage, error); }
        }
        public static void Run(string stage, Action action)
        { Run<object>(stage, delegate { action(); return null; }); }

        // A cleanup failure must not overwrite the error that led to cleanup.
        public static T WithCleanup<T>(Func<T> action, Action cleanup)
        {
            Exception primary = null;
            try { return action(); }
            catch (Exception error) { primary = error; throw; }
            finally {
                try { cleanup(); }
                catch (Exception error) {
                    if (primary != null) throw new AggregateException("Native operation and cleanup failed.", primary, error);
                    throw;
                }
            }
        }

        private sealed class Failure
        {
            internal string Stage;
            internal Exception Error;
        }
        private static void Collect(Exception error, string stage, List<Failure> failures, int depth, ref bool truncated)
        {
            if (error == null) return;
            if (failures.Count >= MaxFailures || depth > MaxDepth) { truncated = true; return; }
            var marked = error as NativeStageException;
            if (marked != null) { Collect(marked.InnerException, marked.Stage, failures, depth + 1, ref truncated); return; }
            var aggregate = error as AggregateException;
            if (aggregate != null) {
                foreach (Exception child in aggregate.InnerExceptions) {
                    if (failures.Count >= MaxFailures) { truncated = true; break; }
                    Collect(child, stage, failures, depth + 1, ref truncated);
                }
                return;
            }
            failures.Add(new Failure { Stage = StageKey(stage), Error = error });
        }

        private static Win32Exception NativeError(Exception error)
        {
            for (int depth = 0; error != null && depth <= MaxDepth; depth++, error = error.InnerException) {
                var native = error as Win32Exception;
                if (native != null) return native;
            }
            return null;
        }

        private static Explanation Explain(Exception error)
        {
            Explanation explanation;
            if (Known.TryGetValue(error.Message, out explanation)) return explanation;
            if (error is FileNotFoundException || error is DirectoryNotFoundException)
                return new Explanation("file-missing", "이 단계에서 필요한 파일 또는 폴더를 찾지 못했습니다.", "앱 전체를 압축 해제했는지 확인하세요. 복구 단계라면 기존 실습 폴더와 소유 기록을 담당자가 확인해야 합니다.");
            if (error is UnauthorizedAccessException)
                return new Explanation("access-denied", "파일 또는 폴더에 접근할 권한이 없습니다.", "현재 Windows 계정의 접근 권한과 보안 프로그램의 차단 여부를 확인하세요.");
            if (error is IOException)
                return new Explanation("file-io", "파일 작업에 실패했습니다. 다른 프로세스가 파일을 사용 중일 수 있습니다.", "해당 실습을 사용하는 프로그램을 종료하고 아래 오류 번호를 담당자에게 전달하세요.");
            return new Explanation("unexpected", "이 단계에서 실행을 계속할 수 없는 오류가 발생했습니다.", "아래 단계·예외 유형·오류 번호를 담당자에게 전달해 주세요.");
        }

        public static string Format(Exception error)
        {
            if (error == null) throw new ArgumentNullException("error");
            var failures = new List<Failure>(); bool truncated = false;
            Collect(error, "unknown", failures, 0, ref truncated);
            var text = new StringBuilder("Autograde IDE 진단\n");
            for (int index = 0; index < failures.Count; index++) {
                Failure failure = failures[index]; Explanation detail = Explain(failure.Error);
                text.Append(index == 0 ? "\n처음 확인된 오류\n" : "\n추가 오류\n");
                text.Append("단계: ").Append(Stages[failure.Stage]).Append(" [").Append(failure.Stage).Append("]\n");
                text.Append("진단 코드: native.").Append(failure.Stage).Append('.').Append(detail.Code).Append('\n');
                text.Append("예외 유형: ").Append(failure.Error.GetType().FullName).Append('\n');
                text.Append("HResult: 0x").Append(failure.Error.HResult.ToString("X8", CultureInfo.InvariantCulture)).Append('\n');
                Win32Exception native = NativeError(failure.Error);
                if (native != null) text.Append("Windows 오류 번호: ").Append(native.NativeErrorCode.ToString(CultureInfo.InvariantCulture)).Append('\n');
                text.Append("원인: ").Append(detail.Reason).Append('\n');
                text.Append("조치: ").Append(detail.Action).Append('\n');
            }
            if (truncated) text.Append("\n추가 오류는 진단 길이 제한으로 생략했습니다.\n");
            return text.ToString();
        }
    }
}
