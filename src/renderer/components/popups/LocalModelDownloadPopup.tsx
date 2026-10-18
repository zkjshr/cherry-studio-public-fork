import { Download, PictureInPicture2, RefreshCw } from 'lucide-react'
import type React from 'react'
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@cherrystudio/ui'
import { cn } from '@cherrystudio/ui/lib/utils'
import { LocalModelDownloadProgress } from '@renderer/components/LocalModelDownloadProgress'
import { useLocalModel } from '@renderer/hooks/useLocalModel'
import { createPopup, type PopupInjectedProps } from '@renderer/services/popup'
import { toast } from '@renderer/services/toast'
import type { LocalModelBundleId } from '@shared/data/presets/localModel'

export interface LocalModelDownloadParams {
  id: LocalModelBundleId
  /** What is being fetched, e.g. "PaddleOCR PP-OCRv6 · ~140 MB". */
  description: string
}

type Props = LocalModelDownloadParams & PopupInjectedProps<boolean>

const PopupContainer: React.FC<Props> = ({ open, resolve, id, description }) => {
  const { t } = useTranslation()
  const { status, percent, download, cancel } = useLocalModel(id)
  const [failed, setFailed] = useState(false)

  const downloading = status === 'downloading'

  const handleDownload = useCallback(async () => {
    setFailed(false)

    try {
      // download() resolves true once the model is on disk and false when the user
      // cancels it — which is already exactly this dialog's answer.
      resolve(await download())
    } catch {
      // Stay open so the user can retry without re-opening the dropdown; the cause is
      // already logged by the main-process download service.
      toast.error(t('settings.dependencies.localModels.notice.downloadFailed'))
      setFailed(true)
    }
  }, [download, resolve, t])

  const handleCancel = useCallback(() => {
    if (!downloading) {
      resolve(false)
      return
    }

    // Do not resolve here: let the in-flight download() settle the dialog once main
    // confirms the cancellation, so the bar cannot be left frozen on screen.
    void cancel()
  }, [cancel, downloading, resolve])

  const handleBackground = useCallback(() => {
    // 关闭弹窗但让下载继续跑（主进程持有任务）；解析 false——调用方只当"模型还没
    // 就绪"继续走，下载进度与完成状态由设置页的模型区（LocalModelRequirement）展示。
    toast.success(t('knowledge.rag.download_background_toast'))
    resolve(false)
  }, [resolve, t])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) {
        handleCancel()
      }
    },
    [handleCancel]
  )

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        showCloseButton={false}
        // Downloading 时点遮罩 = 后台下载（与按钮同义），不误伤下载任务。
        closeOnOverlayClick={true}
        overlayClassName="z-[90]"
        className={cn('confirm-popup z-[90] gap-5 sm:max-w-lg')}
        onInteractOutside={(event) => {
          if (downloading) {
            // 拦掉默认的 dismiss，改走后台下载路径——否则 onOpenChange(false) 会
            // 落到 handleCancel 把下载取消掉。
            event.preventDefault()
            handleBackground()
          }
        }}>
        <DialogHeader className="gap-3">
          <div className="flex items-start gap-3">
            <Download className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <DialogTitle className="text-base leading-6">{t('knowledge.rag.download_local_model')}</DialogTitle>
              <DialogDescription asChild>
                <div className="mt-2 max-w-full min-w-0 text-sm leading-5 wrap-anywhere text-muted-foreground">
                  {description}
                </div>
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        {downloading ? <LocalModelDownloadProgress percent={percent} /> : null}

        <DialogFooter>
          {downloading ? (
            <Button variant="outline" onClick={handleBackground} className="gap-1" data-ui="local-model.background">
              <PictureInPicture2 className="size-4" />
              {t('knowledge.rag.download_background')}
            </Button>
          ) : null}
          <Button variant="outline" onClick={handleCancel}>
            {t('common.cancel')}
          </Button>
          {downloading ? null : (
            <Button onClick={() => void handleDownload()} className="gap-1">
              {failed ? <RefreshCw className="size-4" /> : <Download className="size-4" />}
              {t(failed ? 'common.retry' : 'settings.dependencies.localModels.download')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Ask to download a local model, then stay open and report the download.
 *
 * Resolves true only once the model is on disk, so a caller can gate on it — picking a
 * processor whose model never arrived would just fail later in the job. 下载中可以
 * 「后台下载」收起弹窗（解析 false）：任务在主进程继续，进度在设置页的模型区可见。
 *
 * Not reusable as ConfirmActionPopup: that one disables Cancel while its action runs,
 * and a ~140 MB download has to stay interruptible.
 */
const LocalModelDownloadPopup = createPopup<LocalModelDownloadParams, boolean>(PopupContainer, {
  dismissResult: false
})

export default LocalModelDownloadPopup
