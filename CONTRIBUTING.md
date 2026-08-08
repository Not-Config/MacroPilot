# Участие в разработке

Спасибо за интерес к MacroPilot.

## Ошибки и предложения

Перед созданием Issue проверьте, что похожего сообщения ещё нет. Для ошибки укажите:

- версию MacroPilot и Windows;
- способ запуска: `run_windows.bat` или EXE;
- игру или приложение;
- минимальную последовательность действий;
- полный текст ошибки без паролей и других чувствительных данных.

## Изменения кода

1. Создайте отдельную ветку.
2. Не добавляйте `.venv`, `build`, `dist` и личные макросы.
3. Запустите проверки:

```bash
python -m unittest discover -s tests -v
python -m py_compile main.py qt_app.py qt_graph.py graph_model.py macro_core.py app_settings.py visual_script.py image_matcher.py ocr_reader.py windows_input.py update_service.py project_config.py
```

4. В Pull Request кратко опишите причину, изменения и результат тестов.

Отправляя изменения в проект, вы соглашаетесь на их распространение на условиях [лицензии MIT](LICENSE).
