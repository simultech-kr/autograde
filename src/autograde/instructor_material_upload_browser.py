"""Progressive batch upload: retain selected files on failure, never auto-apply."""

SCRIPT = r"""
  document.querySelectorAll('form[data-material-batch-upload]').forEach(form => {
    const input = form.querySelector('input[type=file]');
    const status = form.querySelector('[data-upload-progress]');
    const meter = form.querySelector('[data-upload-meter]');
    const submit = form.querySelector('button[type=submit],button:not([type])');
    if (!input || !status || !meter || !submit) return;
    let busy = false;
    const say = text => { status.textContent = text; };
    const finish = text => {
      busy = false; submit.disabled = false; form.removeAttribute('aria-busy');
      meter.hidden = true; say(text);
    };
    window.addEventListener('beforeunload', event => {
      if (busy) { event.preventDefault(); event.returnValue = ''; }
    });
    input.addEventListener('change', () => {
      const file = input.files[0];
      say(file ? `${file.name} · ${(file.size / 1048576).toFixed(2)} MiB 선택됨. 업로드 후 학생 매칭을 검토합니다.` :
        'ZIP 선택 → 업로드·검사 → 학생 매칭 검토 → 반영');
    });
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (busy) return;
      const file = input.files[0];
      if (!file || !file.name.toLowerCase().endsWith('.zip') || file.size <= 0 || file.size > 100 * 1048576) {
        say('1바이트 이상, 100 MiB 이하 ZIP 파일을 선택하세요. 서버에는 아직 전송하지 않았습니다.');
        return;
      }
      if (guarded.some(other => other !== form && dirty(other)) &&
          !window.confirm('다른 영역에 저장하지 않은 내용이 있습니다. 먼저 저장하지 않고 검토 화면으로 이동할까요?')) return;
      busy = true; submit.disabled = true; form.setAttribute('aria-busy', 'true');
      meter.hidden = false; meter.value = 0; say('ZIP 업로드 중 · 아직 학생 자료는 변경되지 않습니다.');
      const request = new XMLHttpRequest();
      request.open('POST', form.action, true);
      request.timeout = 120000;
      request.upload.addEventListener('progress', event => {
        if (event.lengthComputable) {
          meter.value = Math.round(100 * event.loaded / event.total);
          say(`ZIP 업로드 중 ${meter.value}% · 아직 학생 자료는 변경되지 않습니다.`);
        }
      });
      request.upload.addEventListener('load', () => {
        meter.removeAttribute('value'); say('업로드 완료 · 파일 형식과 학번을 검사하고 있습니다.');
      });
      const uncertain = () => finish('업로드 결과를 확인하지 못했습니다. 선택한 파일은 이 화면에 유지됩니다. '
        + '학생 자료는 반영되지 않았습니다. 자료 화면의 “학생 매칭 검토 이어가기”를 확인하거나 다시 업로드하세요.');
      request.addEventListener('error', uncertain);
      request.addEventListener('timeout', uncertain);
      request.addEventListener('abort', uncertain);
      request.addEventListener('load', () => {
        const next = new URL(request.responseURL || form.action, window.location.href);
        const prefix = new URL(form.action).pathname.replace(/\/upload-batch$/, '/batch-review/');
        if (request.status === 200 && next.origin === window.location.origin &&
            next.pathname.startsWith(prefix) && /^mbatch_[0-9a-f]{32}$/.test(next.pathname.slice(prefix.length))) {
          finish('검사 완료 · 학생별 매칭 검토로 이동합니다.');
          if (guarded.some(other => other !== form && dirty(other)) &&
              !window.confirm('업로드하는 동안 다른 영역이 변경되었습니다. 저장하지 않고 검토 화면으로 이동할까요?')) {
            say('검토본은 서버에 저장되었습니다. 다른 영역을 먼저 저장한 뒤 학생 매칭 검토를 계속하세요.');
            const link = document.createElement('a');
            link.href = next.href; link.textContent = '학생 매칭 검토 이어가기';
            status.append(' ', link); return;
          }
          leaving = true; window.location.assign(next.href); return;
        }
        if (request.status === 413) {
          finish('웹서버가 업로드 크기를 제한했습니다(413). 선택한 파일은 유지됩니다. '
            + '파일을 나누거나 운영자에게 자료 일괄 업로드 경로의 허용 크기를 확인해 달라고 요청하세요.'); return;
        }
        if ([401, 403].includes(request.status)) {
          finish('로그인 상태를 확인할 수 없습니다. 선택한 파일은 유지됩니다. 새 탭에서 교수자 로그인을 확인한 뒤 다시 시도하세요.'); return;
        }
        let reason = '';
        if (request.responseText) {
          const response = new DOMParser().parseFromString(request.responseText, 'text/html');
          const alert = response.querySelector('.error,[role=alert]');
          if (alert) reason = alert.textContent.trim().slice(0, 2000);
        }
        finish((reason || '파일 검사에 실패했습니다. ZIP 구조와 파일명을 확인하세요.') +
          ' 기존 학생 자료는 유지됩니다. 수정한 파일을 다시 선택해 업로드하세요.');
      });
      try { request.send(new FormData(form)); } catch (_) { uncertain(); }
    });
  });
"""
