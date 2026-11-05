// Windows-only private configuration reader and bounded HTTPS policy transport.
using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Authentication;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace Autograde.NativeWindows
{
    internal static class NativeCleanupPolicyWindows
    {
        internal const int MaxBytes = 4096, TimeoutMilliseconds = 3000;
        public static NativeCleanupPolicy Load()
        { return NativeCleanupPolicy.Resolve(ReadConfig, Fetch); }
        private static IDictionary<string, object> Json(string text)
        {
            if (Encoding.UTF8.GetByteCount(text) > MaxBytes) throw new InvalidDataException("Operator policy data is too large.");
            var value = new JavaScriptSerializer { MaxJsonLength = MaxBytes, RecursionLimit = 8 }.DeserializeObject(text) as Dictionary<string, object>;
            if (value == null) throw new InvalidDataException("Invalid operator policy data.");
            return value;
        }
        private static NativeSeatConfig ReadConfig()
        {
            string directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Autograde Operator");
            string file = Path.Combine(directory, "device.json");
            if (!NativePaths.Exists(file)) return null;
            NativePaths.PrivateDirectory(directory, false);
            return NativeCleanupPolicy.ParseConfig(Json(NativePaths.ReadPrivateText(file, MaxBytes)));
        }
        private static IDictionary<string, object> Fetch(NativeSeatConfig config)
        { return FetchAsync(config).GetAwaiter().GetResult(); }
        private static async Task<IDictionary<string, object>> FetchAsync(NativeSeatConfig config)
        {
            // Default certificate verification remains enabled. Never follow a redirect
            // carrying this device bearer token, including a same-origin redirect.
            using (var handler = new HttpClientHandler { AllowAutoRedirect = false, UseCookies = false, UseDefaultCredentials = false, SslProtocols = SslProtocols.Tls12 })
            using (var client = new HttpClient(handler))
            using (var cancel = new CancellationTokenSource()) {
                client.Timeout = TimeSpan.FromMilliseconds(TimeoutMilliseconds);
                cancel.CancelAfter(TimeoutMilliseconds);
                var request = new HttpRequestMessage(HttpMethod.Get, new Uri(config.ApiUrl, "/v1/operations/seats/" + config.SeatId + "/policy"));
                using (request) {
                    request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", config.ReportToken);
                    // The outer deadline also bounds platform DNS/request implementations
                    // that fail to promptly observe the cancellation token.
                    var work = ReadResponse(client, request, cancel.Token);
                    if (await Task.WhenAny(work, Task.Delay(TimeoutMilliseconds)).ConfigureAwait(false) != work) {
                        cancel.Cancel();
                        ObserveFailure(work);
                        throw new TimeoutException("Operator policy request timed out.");
                    }
                    return await work.ConfigureAwait(false);
                }
            }
        }
        private static void ObserveFailure(Task work)
        { work.ContinueWith(task => { var ignored = task.Exception; }, TaskContinuationOptions.OnlyOnFaulted); }
        private static async Task<IDictionary<string, object>> ReadResponse(HttpClient client, HttpRequestMessage request, CancellationToken cancel)
        {
            using (var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancel).ConfigureAwait(false)) {
                if ((int)response.StatusCode != 200 || (response.Content.Headers.ContentLength.HasValue && response.Content.Headers.ContentLength.Value > MaxBytes))
                    throw new InvalidDataException("Operator policy response was not accepted.");
                using (var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                using (var data = new MemoryStream()) {
                    byte[] buffer = new byte[1024]; int count;
                    while ((count = await stream.ReadAsync(buffer, 0, Math.Min(buffer.Length, MaxBytes + 1 - (int)data.Length), cancel).ConfigureAwait(false)) != 0) {
                        data.Write(buffer, 0, count);
                        if (data.Length > MaxBytes) throw new InvalidDataException("Operator policy response is too large.");
                    }
                    return Json(new UTF8Encoding(false, true).GetString(data.ToArray()));
                }
            }
        }
    }
}
